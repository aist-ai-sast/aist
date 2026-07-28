from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft202012Validator, SchemaError
from jsonschema import ValidationError as JsonSchemaValidationError


class DastConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DastOnboardingBundle:

    """Versioned one-shot onboarding input; token is never included in output snapshots."""

    bundle_version: int
    config: DastIntegrationConfig
    token: str = field(repr=False)

    VERSION: ClassVar[int] = 1
    MAX_BUNDLE_BYTES: ClassVar[int] = 128 * 1024
    MAX_CA_BUNDLE_BYTES: ClassVar[int] = 64 * 1024
    MAX_TOKEN_BYTES: ClassVar[int] = 4096
    _FIELDS = frozenset(
        {
            "bundle_version",
            "gateway_url",
            "ca_bundle",
            "contract_major",
            "integrator_public_id",
            "server_fingerprint",
            "token",
        },
    )

    @classmethod
    def from_json(cls, raw: str | bytes) -> DastOnboardingBundle:
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not isinstance(raw_bytes, bytes):
            msg = "DAST onboarding bundle must be UTF-8 JSON."
            raise DastConfigError(msg)
        if len(raw_bytes) > cls.MAX_BUNDLE_BYTES:
            msg = "DAST onboarding bundle exceeds the size limit."
            raise DastConfigError(msg)

        def reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    msg = f"Duplicate DAST onboarding bundle field: {key}."
                    raise DastConfigError(msg)
                result[key] = value
            return result

        try:
            payload = json.loads(raw_bytes, object_pairs_hook=reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = "DAST onboarding bundle must be valid UTF-8 JSON."
            raise DastConfigError(msg) from exc
        return cls.from_mapping(payload)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> DastOnboardingBundle:
        if not isinstance(payload, Mapping):
            msg = "DAST onboarding bundle must be a JSON object."
            raise DastConfigError(msg)
        keys = set(payload)
        missing = cls._FIELDS - keys
        unknown = keys - cls._FIELDS
        if missing:
            msg = f"Missing DAST onboarding bundle fields: {', '.join(sorted(missing))}."
            raise DastConfigError(msg)
        if unknown:
            msg = f"Unknown DAST onboarding bundle fields: {', '.join(sorted(unknown))}."
            raise DastConfigError(msg)
        bundle_version = payload["bundle_version"]
        if isinstance(bundle_version, bool) or bundle_version != cls.VERSION:
            msg = f"Only DAST onboarding bundle_version {cls.VERSION} is supported."
            raise DastConfigError(msg)
        ca_bundle = payload["ca_bundle"]
        if not isinstance(ca_bundle, str):
            msg = "ca_bundle must be a string."
            raise DastConfigError(msg)
        if len(ca_bundle.encode("utf-8")) > cls.MAX_CA_BUNDLE_BYTES:
            msg = "ca_bundle exceeds the size limit."
            raise DastConfigError(msg)
        if "PRIVATE KEY" in ca_bundle.upper():
            msg = "ca_bundle must contain public trust material only."
            raise DastConfigError(msg)
        normalized_ca_bundle = ca_bundle.strip()
        if (
            normalized_ca_bundle
            and re.fullmatch(
                r"(?:-----BEGIN CERTIFICATE-----\s+.+?\s+-----END CERTIFICATE-----\s*)+",
                normalized_ca_bundle,
                flags=re.DOTALL,
            )
            is None
        ):
            msg = "ca_bundle must contain only PEM-encoded public certificates."
            raise DastConfigError(msg)
        token = payload["token"]
        if not isinstance(token, str) or not token or token != token.strip():
            msg = "token must be a non-empty string."
            raise DastConfigError(msg)
        if len(token.encode("utf-8")) > cls.MAX_TOKEN_BYTES:
            msg = "token exceeds the size limit."
            raise DastConfigError(msg)
        config = DastIntegrationConfig.from_snapshot({key: payload[key] for key in DastIntegrationConfig._FIELDS})
        return cls(bundle_version=bundle_version, config=config, token=token)

    def to_safe_snapshot(self) -> dict[str, Any]:
        return {
            "bundle_version": self.bundle_version,
            **self.config.to_snapshot(),
        }


@dataclass(frozen=True, slots=True)
class DastIntegrationConfig:
    gateway_url: str
    ca_bundle: str
    contract_major: int
    integrator_public_id: str
    server_fingerprint: str

    _FIELDS = frozenset(
        {
            "gateway_url",
            "ca_bundle",
            "contract_major",
            "integrator_public_id",
            "server_fingerprint",
        },
    )

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> DastIntegrationConfig:
        if not isinstance(snapshot, Mapping):
            msg = "DAST integration config must be a JSON object."
            raise DastConfigError(msg)
        keys = set(snapshot)
        missing = cls._FIELDS - keys
        unknown = keys - cls._FIELDS
        if missing:
            msg = f"Missing DAST integration config fields: {', '.join(sorted(missing))}."
            raise DastConfigError(msg)
        if unknown:
            msg = f"Unknown DAST integration config fields: {', '.join(sorted(unknown))}."
            raise DastConfigError(msg)

        gateway_url = cls._validate_gateway_url(snapshot["gateway_url"])
        ca_bundle = cls._required_string(snapshot["ca_bundle"], "ca_bundle", allow_blank=True)
        contract_major = snapshot["contract_major"]
        if isinstance(contract_major, bool) or not isinstance(contract_major, int):
            msg = "contract_major must be an integer."
            raise DastConfigError(msg)
        if contract_major != 2:
            msg = "Only DAST contract_major 2 is supported."
            raise DastConfigError(msg)
        integrator_public_id = cls._required_string(snapshot["integrator_public_id"], "integrator_public_id")
        server_fingerprint = cls._required_string(snapshot["server_fingerprint"], "server_fingerprint")
        return cls(
            gateway_url=gateway_url,
            ca_bundle=ca_bundle,
            contract_major=contract_major,
            integrator_public_id=integrator_public_id,
            server_fingerprint=server_fingerprint,
        )

    @staticmethod
    def _required_string(value: Any, field: str, *, allow_blank: bool = False) -> str:
        if not isinstance(value, str):
            msg = f"{field} must be a string."
            raise DastConfigError(msg)
        normalized = value.strip()
        if not allow_blank and not normalized:
            msg = f"{field} must not be blank."
            raise DastConfigError(msg)
        return normalized

    @classmethod
    def _validate_gateway_url(cls, value: Any) -> str:
        gateway_url = cls._required_string(value, "gateway_url")
        parsed = urlsplit(gateway_url)
        if parsed.scheme != "https" or not parsed.hostname:
            msg = "gateway_url must be an absolute HTTPS URL."
            raise DastConfigError(msg)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            msg = "gateway_url cannot contain credentials, a query, or a fragment."
            raise DastConfigError(msg)
        normalized_path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "gateway_url": self.gateway_url,
            "ca_bundle": self.ca_bundle,
            "contract_major": self.contract_major,
            "integrator_public_id": self.integrator_public_id,
            "server_fingerprint": self.server_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class DastTargetSnapshot:
    provider_id: str
    display_name: str
    contract_revision: str
    capability_revision: str
    schema_digest: str
    parameter_schema: dict[str, Any]
    provider_defaults: dict[str, Any]
    repository_keys: tuple[str, ...]
    autonomous_ready: bool

    _FIELDS = frozenset(
        {
            "id",
            "display_name",
            "contract_revision",
            "capability_revision",
            "schema_digest",
            "parameter_schema",
            "defaults",
            "repository_keys",
            "autonomous_ready",
        },
    )

    # These mirror the column widths in aist.models.DastTarget, and a test holds them in step. A
    # catalog is tenant-supplied, so an oversized value has to be refused here -- as a catalog error
    # naming the field -- instead of reaching storage and failing an atomic refresh with a database
    # error that says nothing about which target or field was at fault.
    MAX_PROVIDER_ID_LENGTH: ClassVar[int] = 255
    MAX_DISPLAY_NAME_LENGTH: ClassVar[int] = 255
    MAX_CONTRACT_REVISION_LENGTH: ClassVar[int] = 64
    MAX_CAPABILITY_REVISION_LENGTH: ClassVar[int] = 96
    MAX_SCHEMA_DIGEST_LENGTH: ClassVar[int] = 96

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> DastTargetSnapshot:
        if not isinstance(snapshot, Mapping):
            msg = "DAST target snapshot must be a JSON object."
            raise DastConfigError(msg)
        keys = set(snapshot)
        missing = cls._FIELDS - keys
        unknown = keys - cls._FIELDS
        if missing:
            msg = f"Missing DAST target fields: {', '.join(sorted(missing))}."
            raise DastConfigError(msg)
        if unknown:
            msg = f"Unknown DAST target fields: {', '.join(sorted(unknown))}."
            raise DastConfigError(msg)

        parameter_schema = cls._strict_parameter_schema(snapshot["parameter_schema"])
        provider_defaults = cls._json_object(snapshot["defaults"], "defaults")
        cls._validate_parameters(parameter_schema, provider_defaults, "defaults")
        repository_keys = snapshot["repository_keys"]
        if (
            not isinstance(repository_keys, list)
            or not repository_keys
            or any(not isinstance(key, str) or not key.strip() for key in repository_keys)
        ):
            msg = "repository_keys must be a non-empty list of non-blank strings."
            raise DastConfigError(msg)
        normalized_repository_keys = tuple(dict.fromkeys(key.strip() for key in repository_keys))
        autonomous_ready = snapshot["autonomous_ready"]
        if not isinstance(autonomous_ready, bool):
            msg = "autonomous_ready must be a boolean."
            raise DastConfigError(msg)

        return cls(
            provider_id=cls._bounded_string(snapshot["id"], "id", cls.MAX_PROVIDER_ID_LENGTH),
            display_name=cls._bounded_string(
                snapshot["display_name"],
                "display_name",
                cls.MAX_DISPLAY_NAME_LENGTH,
            ),
            contract_revision=cls._bounded_string(
                snapshot["contract_revision"],
                "contract_revision",
                cls.MAX_CONTRACT_REVISION_LENGTH,
            ),
            capability_revision=cls._bounded_string(
                snapshot["capability_revision"],
                "capability_revision",
                cls.MAX_CAPABILITY_REVISION_LENGTH,
            ),
            schema_digest=cls._bounded_string(
                snapshot["schema_digest"],
                "schema_digest",
                cls.MAX_SCHEMA_DIGEST_LENGTH,
            ),
            parameter_schema=parameter_schema,
            provider_defaults=provider_defaults,
            repository_keys=normalized_repository_keys,
            autonomous_ready=autonomous_ready,
        )

    @classmethod
    def _bounded_string(cls, value: Any, field: str, limit: int) -> str:
        text = DastIntegrationConfig._required_string(value, field)
        if len(text) > limit:
            msg = f"{field} exceeds {limit} characters."
            raise DastConfigError(msg)
        return text

    @staticmethod
    def _json_object(value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            msg = f"{field} must be a JSON object."
            raise DastConfigError(msg)
        return deepcopy(value)

    @classmethod
    def _strict_parameter_schema(cls, value: Any) -> dict[str, Any]:
        schema = cls._json_object(value, "parameter_schema")
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            msg = "parameter_schema must declare JSON Schema Draft 2020-12."
            raise DastConfigError(msg)
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            msg = "parameter_schema must describe a strict object with additionalProperties=false."
            raise DastConfigError(msg)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            msg = "parameter_schema is not a valid Draft 2020-12 schema."
            raise DastConfigError(msg) from exc
        return schema

    @staticmethod
    def _validate_parameters(schema: dict[str, Any], parameters: dict[str, Any], field: str) -> None:
        try:
            Draft202012Validator(schema).validate(parameters)
        except JsonSchemaValidationError as exc:
            msg = f"{field} does not match parameter_schema."
            raise DastConfigError(msg) from exc

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "display_name": self.display_name,
            "contract_revision": self.contract_revision,
            "capability_revision": self.capability_revision,
            "schema_digest": self.schema_digest,
            "parameter_schema": deepcopy(self.parameter_schema),
            "defaults": deepcopy(self.provider_defaults),
            "repository_keys": list(self.repository_keys),
            "autonomous_ready": self.autonomous_ready,
        }


@dataclass(frozen=True, slots=True)
class DastBindingParameters:
    values: dict[str, Any]

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, Any],
        *,
        target: DastTargetSnapshot,
    ) -> DastBindingParameters:
        values = DastTargetSnapshot._json_object(snapshot, "parameter_snapshot")
        DastTargetSnapshot._validate_parameters(target.parameter_schema, values, "parameter_snapshot")
        return cls(values=values)

    def to_snapshot(self) -> dict[str, Any]:
        return deepcopy(self.values)
