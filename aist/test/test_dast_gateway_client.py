import json
import stat
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import requests
from django.test import SimpleTestCase

from aist.integrations.dast_config import DastIntegrationConfig
from aist.integrations.dast_gateway_client import (
    DastGatewayClient,
    DastGatewayClientError,
    scoped_dast_gateway_client,
)

TOKEN = "pub_aist.secretvaluevaluevalue"  # noqa: S105


class CallerError(RuntimeError):
    pass


def _config(**overrides):
    payload = {
        "gateway_url": "https://gateway.example",
        "ca_bundle": "",
        "contract_major": 2,
        "integrator_public_id": "pub_aist",
        "server_fingerprint": "sha256:server-fingerprint",
    }
    payload.update(overrides)
    return DastIntegrationConfig.from_snapshot(payload)


def _target(**overrides):
    payload = {
        "id": "webapp",
        "display_name": "Web application",
        "contract_revision": "2.0",
        "capability_revision": "sha256:" + "b" * 64,
        "schema_digest": "sha256:" + "c" * 64,
        "parameter_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        "defaults": {},
        "repository_keys": ["source"],
        "autonomous_ready": True,
    }
    payload.update(overrides)
    return payload


class FakeResponse:

    def __init__(self, status_code, payload=None, *, headers=None, raw_body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = raw_body if raw_body is not None else json.dumps(payload).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        del chunk_size
        yield self._body

    def close(self):
        self.closed = True


class DastGatewayClientTests(SimpleTestCase):

    def _client(self, session):
        return DastGatewayClient(
            session=session,
            config=_config(),
            token=TOKEN,
            verify=True,
            trusted_vpn=False,
            resolver=lambda _host, _port: ("93.184.216.34",),
        )

    def test_ping_uses_v2_fixed_path_tls_verification_timeouts_and_no_redirects(self):
        session = MagicMock(spec=requests.Session)
        response = FakeResponse(
            200,
            {"contract_version": "2.0", "gateway_version": "2026.7", "status": "ok"},
            headers={"Content-Type": "application/json"},
        )
        session.get.return_value = response

        ping = self._client(session).ping()

        self.assertEqual(ping.contract_version, "2.0")
        session.get.assert_called_once_with(
            "https://gateway.example/integrations/v2/ping",
            headers={"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"},
            timeout=(3.05, 10.0),
            verify=True,
            allow_redirects=False,
            stream=True,
        )
        self.assertTrue(response.closed)

    def test_catalog_is_typed_and_supports_conditional_request_on_same_session(self):
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = (
            FakeResponse(
                200,
                {"contract_version": "2.0", "gateway_version": "2026.7", "status": "ok"},
                headers={"Content-Type": "application/json"},
            ),
            FakeResponse(
                200,
                {"contract_version": "2.0", "etag": "catalog-1", "targets": [_target()]},
                headers={"Content-Type": "application/json", "ETag": '"catalog-1"'},
            ),
        )
        client = self._client(session)

        client.ping()
        catalog = client.catalog(etag="catalog-0")

        self.assertEqual(catalog.targets[0].provider_id, "webapp")
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args.kwargs["headers"]["If-None-Match"], "catalog-0")
        for runtime_method in ("start", "status", "logs", "result", "stop"):
            self.assertFalse(hasattr(client, runtime_method))

    def test_deeply_nested_body_is_an_invalid_response_not_an_internal_failure(self):
        """
        Nesting depth is bounded by neither the byte cap nor the target count, so a tiny body can
        still exhaust the JSON parser. The resulting error is not a transport error, and letting it
        escape would record the sync as an opaque internal failure instead of a bad response.
        """
        session = MagicMock(spec=requests.Session)
        # ~20 KB against a 2 MB cap, so nothing about the size is suspicious.
        session.get.return_value = FakeResponse(
            200,
            headers={"Content-Type": "application/json"},
            raw_body=b"[" * 10000 + b"]" * 10000,
        )

        with self.assertRaises(DastGatewayClientError) as caught:
            self._client(session).catalog()

        self.assertEqual(caught.exception.code, "RESPONSE_JSON_INVALID")

    def test_catalog_accepts_a_transport_etag_the_gateway_did_not_write(self):
        """
        A compressing proxy in front of the gateway re-tags the response it re-encodes, and HTTP
        allows it: the header validates the representation that was served, while the body carries
        the catalog's own tag. Cross-checking the two rejected every catalog behind such a proxy.
        """
        session = MagicMock(spec=requests.Session)
        session.get.return_value = FakeResponse(
            200,
            {"contract_version": "2.0", "etag": "catalog-1", "targets": [_target()]},
            headers={"Content-Type": "application/json", "ETag": '"catalog-1-gzip"'},
        )

        catalog = self._client(session).catalog()

        # The stored tag is the body's, so a later conditional request replays what the gateway knows.
        self.assertEqual(catalog.etag, "catalog-1")
        self.assertEqual(catalog.targets[0].provider_id, "webapp")

    def test_catalog_304_returns_typed_not_modified_result(self):
        session = MagicMock(spec=requests.Session)
        session.get.return_value = FakeResponse(304)

        catalog = self._client(session).catalog(etag="catalog-1")

        self.assertTrue(catalog.not_modified)
        self.assertEqual(catalog.etag, "catalog-1")

    def test_redirect_oversize_and_contract_errors_are_redacted(self):
        responses = (
            (FakeResponse(302), "REDIRECT_REJECTED"),
            (
                FakeResponse(
                    200,
                    headers={"Content-Type": "application/json"},
                    raw_body=b"x" * (DastGatewayClient.MAX_RESPONSE_BYTES + 1),
                ),
                "RESPONSE_TOO_LARGE",
            ),
            (
                FakeResponse(
                    200,
                    {"contract_version": "1.0", "gateway_version": "legacy", "status": "ok"},
                    headers={"Content-Type": "application/json"},
                ),
                "CONTRACT_UNSUPPORTED",
            ),
        )
        for response, expected_code in responses:
            session = MagicMock(spec=requests.Session)
            session.get.return_value = response
            with self.subTest(expected_code=expected_code), self.assertRaises(DastGatewayClientError) as caught:
                self._client(session).ping()
            self.assertEqual(caught.exception.code, expected_code)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_network_exception_is_redacted_and_retryable(self):
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError(f"request Authorization Bearer {TOKEN}")

        with self.assertRaises(DastGatewayClientError) as caught:
            self._client(session).ping()

        self.assertEqual(caught.exception.code, "GATEWAY_UNREACHABLE")
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_tls_failure_is_reported_separately_from_an_unreachable_gateway(self):
        # requests.SSLError subclasses ConnectionError, so without its own branch a certificate or
        # handshake problem reports as GATEWAY_UNREACHABLE and points the operator at the network.
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = requests.exceptions.SSLError(
            f"tlsv1 alert internal error, Authorization Bearer {TOKEN}",
        )

        with self.assertRaises(DastGatewayClientError) as caught:
            self._client(session).ping()

        self.assertEqual(caught.exception.code, "TLS_HANDSHAKE_FAILED")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_insecure_tls_verification_is_rejected(self):
        with self.assertRaises(DastGatewayClientError) as caught:
            DastGatewayClient(
                session=MagicMock(spec=requests.Session),
                config=_config(),
                token=TOKEN,
                verify=False,
                trusted_vpn=False,
                resolver=lambda _host, _port: ("93.184.216.34",),
            )

        self.assertEqual(caught.exception.code, "TLS_VERIFY_REQUIRED")

    def test_scoped_client_closes_session_and_removes_private_ca_file_on_exception(self):
        integration = MagicMock()
        integration.get_dast_config.return_value = _config(
            ca_bundle="-----BEGIN CERTIFICATE-----\npublic-ca\n-----END CERTIFICATE-----",
        )
        integration.secret = TOKEN
        integration.vpn_integration = None
        session = MagicMock(spec=requests.Session)
        lifecycle = []

        @contextmanager
        def scoped_session(*, execution_id):
            lifecycle.append(("open", execution_id))
            try:
                yield session
            finally:
                lifecycle.append(("closed", execution_id))

        integration.scoped_session.side_effect = scoped_session

        with self.assertRaises(CallerError), scoped_dast_gateway_client(
            integration,
            execution_id="validation-1",
            resolver=lambda _host, _port: ("93.184.216.34",),
        ) as client:
            ca_path = Path(client._verify)
            self.assertTrue(ca_path.exists())
            self.assertEqual(stat.S_IMODE(ca_path.stat().st_mode), 0o600)
            raise CallerError

        self.assertFalse(ca_path.exists())
        self.assertEqual(lifecycle, [("open", "validation-1"), ("closed", "validation-1")])
