from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from aist.execution.observability import observe_provider_call
from aist.integrations.dast_config import DastConfigError, DastIntegrationConfig, DastTargetSnapshot
from aist.integrations.dast_endpoint_policy import (
    AddressResolver,
    DastEndpointPolicy,
    DastEndpointPolicyError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from aist.models import OrgIntegration


class DastGatewayClientError(RuntimeError):

    """Redacted transport/contract failure safe for durable state and API responses."""

    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class DastGatewayErrorCode(StrEnum):
    TOKEN_MISSING = "TOKEN_MISSING"  # noqa: S105 - error code, not a credential
    TLS_VERIFY_REQUIRED = "TLS_VERIFY_REQUIRED"
    ENDPOINT_POLICY_REJECTED = "ENDPOINT_POLICY_REJECTED"
    GATEWAY_NOT_READY = "GATEWAY_NOT_READY"
    CATALOG_ETAG_MISMATCH = "CATALOG_ETAG_MISMATCH"
    CATALOG_INVALID = "CATALOG_INVALID"
    GATEWAY_UNREACHABLE = "GATEWAY_UNREACHABLE"
    GATEWAY_REQUEST_FAILED = "GATEWAY_REQUEST_FAILED"
    REDIRECT_REJECTED = "REDIRECT_REJECTED"
    TOKEN_REJECTED = "TOKEN_REJECTED"  # noqa: S105 - error code, not a credential
    GATEWAY_HTTP_ERROR = "GATEWAY_HTTP_ERROR"
    RESPONSE_CONTENT_TYPE_INVALID = "RESPONSE_CONTENT_TYPE_INVALID"
    RESPONSE_SCHEMA_INVALID = "RESPONSE_SCHEMA_INVALID"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    RESPONSE_SIZE_INVALID = "RESPONSE_SIZE_INVALID"
    RESPONSE_JSON_INVALID = "RESPONSE_JSON_INVALID"
    CONTRACT_UNSUPPORTED = "CONTRACT_UNSUPPORTED"
    CONFIG_INVALID = "CONFIG_INVALID"


def _client_error(code: DastGatewayErrorCode, *, retryable: bool = False) -> DastGatewayClientError:
    return DastGatewayClientError(str(code), retryable=retryable)


@dataclass(frozen=True, slots=True)
class DastGatewayPing:

    contract_version: str
    gateway_version: str
    status: str


@dataclass(frozen=True, slots=True)
class DastTargetCatalog:

    contract_version: str
    etag: str
    targets: tuple[DastTargetSnapshot, ...]
    not_modified: bool = False


class DastGatewayClient:

    """V2-only client for onboarding validation and target-catalog synchronization."""

    PING_PATH: ClassVar[str] = "/integrations/v2/ping"
    TARGETS_PATH: ClassVar[str] = "/integrations/v2/targets"
    TIMEOUT: ClassVar[tuple[float, float]] = (3.05, 10.0)
    MAX_RESPONSE_BYTES: ClassVar[int] = 2 * 1024 * 1024
    MAX_TARGETS: ClassVar[int] = 1000
    _PING_FIELDS: ClassVar[frozenset[str]] = frozenset({"contract_version", "gateway_version", "status"})
    _CATALOG_FIELDS: ClassVar[frozenset[str]] = frozenset({"contract_version", "etag", "targets"})

    def __init__(
        self,
        *,
        session: requests.Session,
        config: DastIntegrationConfig,
        token: str,
        verify: str | bool,
        trusted_vpn: bool,
        resolver: AddressResolver | None = None,
    ):
        if not token:
            raise _client_error(DastGatewayErrorCode.TOKEN_MISSING)
        if verify is False:
            raise _client_error(DastGatewayErrorCode.TLS_VERIFY_REQUIRED)
        self._session = session
        self._config = config
        self._token = token
        self._verify = verify
        self._endpoint_policy = DastEndpointPolicy(trusted_vpn=trusted_vpn, resolver=resolver)
        try:
            self._endpoint_policy.validate(config.gateway_url)
        except DastEndpointPolicyError as exc:
            raise _client_error(DastGatewayErrorCode.ENDPOINT_POLICY_REJECTED) from exc
        self._session.trust_env = False
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            allowed_methods=frozenset({"GET"}),
            status_forcelist=(429, 502, 503, 504),
            backoff_factor=0.2,
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self._session.mount("https://", HTTPAdapter(max_retries=retry))

    def ping(self) -> DastGatewayPing:
        started = time.monotonic()
        try:
            payload, _headers = self._get_json(self.PING_PATH)
            self._require_exact_fields(payload, self._PING_FIELDS)
            contract_version = self._contract_version(payload["contract_version"])
            gateway_version = self._required_string(payload["gateway_version"])
            status = self._required_string(payload["status"])
            if status != "ok":
                raise _client_error(DastGatewayErrorCode.GATEWAY_NOT_READY, retryable=True)
            result = DastGatewayPing(
                contract_version=contract_version,
                gateway_version=gateway_version,
                status=status,
            )
        except DastGatewayClientError as exc:
            observe_provider_call(
                operation="ping",
                duration_seconds=time.monotonic() - started,
                error_code=exc.code,
            )
            raise
        observe_provider_call(operation="ping", duration_seconds=time.monotonic() - started)
        return result

    def catalog(self, *, etag: str = "") -> DastTargetCatalog:
        started = time.monotonic()
        try:
            headers = {"If-None-Match": etag} if etag else None
            response = self._get_json(self.TARGETS_PATH, headers=headers, allow_not_modified=True)
            if response is None:
                result = DastTargetCatalog(contract_version="", etag=etag, targets=(), not_modified=True)
            else:
                payload, response_headers = response
                self._require_exact_fields(payload, self._CATALOG_FIELDS)
                contract_version = self._contract_version(payload["contract_version"])
                response_etag = self._required_string(payload["etag"])
                header_etag = response_headers.get("ETag", "").strip('"')
                if header_etag and header_etag != response_etag:
                    raise _client_error(DastGatewayErrorCode.CATALOG_ETAG_MISMATCH)
                raw_targets = payload["targets"]
                if not isinstance(raw_targets, list) or len(raw_targets) > self.MAX_TARGETS:
                    raise _client_error(DastGatewayErrorCode.CATALOG_INVALID)
                try:
                    targets = tuple(DastTargetSnapshot.from_snapshot(item) for item in raw_targets)
                except DastConfigError as exc:
                    raise _client_error(DastGatewayErrorCode.CATALOG_INVALID) from exc
                provider_ids = [target.provider_id for target in targets]
                if len(provider_ids) != len(set(provider_ids)):
                    raise _client_error(DastGatewayErrorCode.CATALOG_INVALID)
                result = DastTargetCatalog(
                    contract_version=contract_version,
                    etag=response_etag,
                    targets=targets,
                )
        except DastGatewayClientError as exc:
            observe_provider_call(
                operation="catalog",
                duration_seconds=time.monotonic() - started,
                error_code=exc.code,
            )
            raise
        observe_provider_call(operation="catalog", duration_seconds=time.monotonic() - started)
        return result

    def _get_json(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        allow_not_modified: bool = False,
    ) -> tuple[dict[str, Any], Mapping[str, str]] | None:
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            **(headers or {}),
        }
        try:
            response = self._session.get(
                f"{self._config.gateway_url}{path}",
                headers=request_headers,
                timeout=self.TIMEOUT,
                verify=self._verify,
                allow_redirects=False,
                stream=True,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise _client_error(DastGatewayErrorCode.GATEWAY_UNREACHABLE, retryable=True) from exc
        except requests.RequestException as exc:
            raise _client_error(DastGatewayErrorCode.GATEWAY_REQUEST_FAILED) from exc

        try:
            if allow_not_modified and response.status_code == 304:
                return None
            try:
                self._endpoint_policy.reject_redirect(response.status_code)
            except DastEndpointPolicyError as exc:
                raise _client_error(DastGatewayErrorCode.REDIRECT_REJECTED) from exc
            if response.status_code in {401, 403}:
                raise _client_error(DastGatewayErrorCode.TOKEN_REJECTED)
            if response.status_code != 200:
                retryable = response.status_code in {429, 502, 503, 504}
                raise _client_error(DastGatewayErrorCode.GATEWAY_HTTP_ERROR, retryable=retryable)
            content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != "application/json":
                raise _client_error(DastGatewayErrorCode.RESPONSE_CONTENT_TYPE_INVALID)
            payload = self._read_bounded_json(response)
            if not isinstance(payload, dict):
                raise _client_error(DastGatewayErrorCode.RESPONSE_SCHEMA_INVALID)
            return payload, response.headers
        finally:
            response.close()

    def _read_bounded_json(self, response) -> Any:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > self.MAX_RESPONSE_BYTES:
                    raise _client_error(DastGatewayErrorCode.RESPONSE_TOO_LARGE)
            except ValueError as exc:
                raise _client_error(DastGatewayErrorCode.RESPONSE_SIZE_INVALID) from exc
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > self.MAX_RESPONSE_BYTES:
                raise _client_error(DastGatewayErrorCode.RESPONSE_TOO_LARGE)
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _client_error(DastGatewayErrorCode.RESPONSE_JSON_INVALID) from exc

    def _contract_version(self, value: Any) -> str:
        contract_version = self._required_string(value)
        try:
            major = int(contract_version.split(".", maxsplit=1)[0])
        except ValueError as exc:
            raise _client_error(DastGatewayErrorCode.CONTRACT_UNSUPPORTED) from exc
        if major != self._config.contract_major:
            raise _client_error(DastGatewayErrorCode.CONTRACT_UNSUPPORTED)
        return contract_version

    @staticmethod
    def _required_string(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise _client_error(DastGatewayErrorCode.RESPONSE_SCHEMA_INVALID)
        return value.strip()

    @staticmethod
    def _require_exact_fields(payload: Mapping[str, Any], expected: frozenset[str]) -> None:
        if set(payload) != expected:
            raise _client_error(DastGatewayErrorCode.RESPONSE_SCHEMA_INVALID)


@contextmanager
def scoped_dast_gateway_client(
    integration: OrgIntegration,
    *,
    execution_id: str,
    resolver: AddressResolver | None = None,
) -> Iterator[DastGatewayClient]:
    """Own one integration-scoped VPN/session/CA lifetime for ping and catalog calls."""
    try:
        config = integration.get_dast_config()
    except DastConfigError as exc:
        raise _client_error(DastGatewayErrorCode.CONFIG_INVALID) from exc
    token = integration.secret or ""
    trusted_vpn = bool(integration.vpn_integration and integration.vpn_integration.is_active)
    with ExitStack() as stack:
        verify: str | bool = True
        if config.ca_bundle:
            ca_file = stack.enter_context(tempfile.NamedTemporaryFile(mode="w", encoding="utf-8"))
            os.fchmod(ca_file.fileno(), 0o600)
            ca_file.write(config.ca_bundle)
            ca_file.flush()
            verify = ca_file.name
        session = stack.enter_context(integration.scoped_session(execution_id=execution_id))
        yield DastGatewayClient(
            session=session,
            config=config,
            token=token,
            verify=verify,
            trusted_vpn=trusted_vpn,
            resolver=resolver,
        )
