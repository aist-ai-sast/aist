"""
Tests for VPN integration: OVPN PEM-block parser, serializer validation,
_assemble_env base64 encoding, key-direction extraction, orphan cleanup,
and vpn_sidecar_context security properties.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from subprocess import CalledProcessError
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.urls import reverse
from dojo.models import Product_Type, Product_Type_Member

from aist.api.org_integrations import _split_ovpn_pem_blocks
from aist.models import OrgIntegration, OrgIntegrationType, Organization
from aist.test.test_api import AISTApiBase
from aist.utils.vpn import _assemble_env, _extract_key_direction, cleanup_orphaned_vpn_containers

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FAKE_CA = "-----BEGIN CERTIFICATE-----\nFAKECA\n-----END CERTIFICATE-----"
_FAKE_CERT = "-----BEGIN CERTIFICATE-----\nFAKECERT\n-----END CERTIFICATE-----"
_FAKE_KEY = "-----BEGIN PRIVATE KEY-----\nFAKEKEY\n-----END PRIVATE KEY-----"
_FAKE_TLS_AUTH = "-----BEGIN OpenVPN Static key V1-----\nFAKETLS\n-----END OpenVPN Static key V1-----"

_BASE_OVPN = (
    "client\n"
    "dev tun\n"
    "proto udp\n"
    "remote vpn.example.com 1194\n"
    "cipher AES-256-GCM\n"
)

_FULL_OVPN_WITH_CERTS = (
    _BASE_OVPN
    + f"<ca>\n{_FAKE_CA}\n</ca>\n"
    + f"<cert>\n{_FAKE_CERT}\n</cert>\n"
    + f"<key>\n{_FAKE_KEY}\n</key>\n"
)


# ---------------------------------------------------------------------------
# Unit tests: _split_ovpn_pem_blocks
# ---------------------------------------------------------------------------

class SplitOvpnPemBlocksTests(AISTApiBase):

    def test_extracts_ca_cert_block(self):
        cleaned, extracted = _split_ovpn_pem_blocks(f"client\nremote host\n<ca>\n{_FAKE_CA}\n</ca>\n")
        self.assertEqual(extracted["ca_cert"], _FAKE_CA)
        self.assertNotIn("<ca>", cleaned)
        self.assertNotIn("FAKECA", cleaned)

    def test_extracts_client_cert_and_key(self):
        ovpn = f"client\n<cert>\n{_FAKE_CERT}\n</cert>\n<key>\n{_FAKE_KEY}\n</key>\n"
        cleaned, extracted = _split_ovpn_pem_blocks(ovpn)
        self.assertEqual(extracted["client_cert"], _FAKE_CERT)
        self.assertEqual(extracted["client_key"], _FAKE_KEY)
        self.assertNotIn("<cert>", cleaned)
        self.assertNotIn("<key>", cleaned)

    def test_extracts_tls_auth_block(self):
        ovpn = f"client\n<tls-auth>\n{_FAKE_TLS_AUTH}\n</tls-auth>\n"
        cleaned, extracted = _split_ovpn_pem_blocks(ovpn)
        self.assertEqual(extracted["tls_auth_key"], _FAKE_TLS_AUTH)
        self.assertNotIn("<tls-auth>", cleaned)

    def test_tls_crypt_maps_to_tls_auth_key_field(self):
        ovpn = f"client\n<tls-crypt>\n{_FAKE_TLS_AUTH}\n</tls-crypt>\n"
        cleaned, extracted = _split_ovpn_pem_blocks(ovpn)
        self.assertIn("tls_auth_key", extracted)
        self.assertNotIn("<tls-crypt>", cleaned)

    def test_tls_crypt_v2_extracted(self):
        """tls-crypt-v2 (OpenVPN 2.5+) must be extracted and recorded correctly."""
        ovpn = f"client\n<tls-crypt-v2>\n{_FAKE_TLS_AUTH}\n</tls-crypt-v2>\n"
        cleaned, extracted = _split_ovpn_pem_blocks(ovpn)
        self.assertIn("tls_auth_key", extracted)
        self.assertEqual(extracted.get("tls_key_type"), "tls-crypt-v2")
        self.assertNotIn("<tls-crypt-v2>", cleaned)

    def test_tls_crypt_v2_key_direction_removed_from_config(self):
        """tls-crypt-v2 does not use key-direction; it must be removed from the cleaned config."""
        ovpn = f"client\nkey-direction 1\n<tls-crypt-v2>\n{_FAKE_TLS_AUTH}\n</tls-crypt-v2>\n"
        cleaned, _ = _split_ovpn_pem_blocks(ovpn)
        self.assertNotIn("key-direction", cleaned)

    def test_key_direction_preserved_in_extracted_when_tls_auth(self):
        """key-direction found in the config body is captured in extracted dict."""
        ovpn = f"client\nkey-direction 0\n<tls-auth>\n{_FAKE_TLS_AUTH}\n</tls-auth>\n"
        _, extracted = _split_ovpn_pem_blocks(ovpn)
        self.assertEqual(extracted.get("tls_key_direction"), "0")

    def test_config_directives_preserved(self):
        ovpn = f"client\nremote vpn.example.com 1194\n<ca>\n{_FAKE_CA}\n</ca>\n"
        cleaned, _ = _split_ovpn_pem_blocks(ovpn)
        self.assertIn("remote vpn.example.com 1194", cleaned)
        self.assertIn("client", cleaned)

    def test_no_blocks_returns_original(self):
        cleaned, extracted = _split_ovpn_pem_blocks(_BASE_OVPN)
        self.assertEqual(extracted, {})
        self.assertIn("remote vpn.example.com 1194", cleaned)

    def test_full_ovpn_all_blocks_extracted(self):
        cleaned, extracted = _split_ovpn_pem_blocks(_FULL_OVPN_WITH_CERTS)
        self.assertIn("ca_cert", extracted)
        self.assertIn("client_cert", extracted)
        self.assertIn("client_key", extracted)
        for tag in ("<ca>", "<cert>", "<key>"):
            self.assertNotIn(tag, cleaned)
        self.assertIn("remote vpn.example.com 1194", cleaned)

    def test_excessive_blank_lines_collapsed(self):
        ovpn = f"client\n\n\n\n<ca>\n{_FAKE_CA}\n</ca>\n\n\n"
        cleaned, _ = _split_ovpn_pem_blocks(ovpn)
        self.assertNotIn("\n\n\n", cleaned)


# ---------------------------------------------------------------------------
# Unit tests: _extract_key_direction
# ---------------------------------------------------------------------------

class KeyDirectionExtractionTests(AISTApiBase):

    def test_extracts_key_direction_0(self):
        ovpn = "client\nkey-direction 0\nremote vpn.example.com 1194\n"
        self.assertEqual(_extract_key_direction(ovpn), "0")

    def test_extracts_key_direction_1(self):
        ovpn = "client\nkey-direction 1\nremote vpn.example.com 1194\n"
        self.assertEqual(_extract_key_direction(ovpn), "1")

    def test_defaults_to_1_when_absent(self):
        self.assertEqual(_extract_key_direction("client\nremote vpn.example.com 1194\n"), "1")

    def test_ignores_invalid_value(self):
        """Values other than 0/1 are not valid; function must default to '1'."""
        self.assertEqual(_extract_key_direction("client\nkey-direction 5\n"), "1")

    def test_empty_string_returns_1(self):
        self.assertEqual(_extract_key_direction(""), "1")


# ---------------------------------------------------------------------------
# Unit tests: _assemble_env base64 encoding
# ---------------------------------------------------------------------------

class AssembleEnvBase64Tests(AISTApiBase):

    def _make_secret(self, **kwargs):
        defaults = {
            "ovpn_content": _BASE_OVPN,
            "ca_cert": _FAKE_CA,
            "client_cert": "",
            "client_key": _FAKE_KEY,
            "tls_auth_key": "",
            "vpn_username": "alice",
            "vpn_password": "s3cr3t",
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_ovpn_content_is_base64(self):
        secret = self._make_secret()
        env = _assemble_env(secret)
        decoded = base64.b64decode(env["AIST_VPN_OVPN_CONTENT"]).decode()
        self.assertEqual(decoded, _BASE_OVPN)

    def test_ca_cert_is_base64(self):
        secret = self._make_secret()
        env = _assemble_env(secret)
        decoded = base64.b64decode(env["AIST_VPN_CA_CERT"]).decode()
        self.assertEqual(decoded, _FAKE_CA)

    def test_client_key_is_base64(self):
        secret = self._make_secret()
        env = _assemble_env(secret)
        decoded = base64.b64decode(env["AIST_VPN_CLIENT_KEY"]).decode()
        self.assertEqual(decoded, _FAKE_KEY)

    def test_base64_values_contain_no_newlines(self):
        """Encoded values must be single-line so docker run -e KEY=VALUE works."""
        secret = self._make_secret()
        env = _assemble_env(secret)
        for key, value in env.items():
            self.assertNotIn("\n", value, f"{key} contains a newline")

    def test_username_and_password_not_base64_encoded(self):
        secret = self._make_secret()
        env = _assemble_env(secret)
        self.assertEqual(env["AIST_VPN_USERNAME"], "alice")
        self.assertEqual(env["AIST_VPN_PASSWORD"], "s3cr3t")

    def test_empty_fields_excluded(self):
        secret = self._make_secret(ca_cert="", client_cert="", tls_auth_key="")
        env = _assemble_env(secret)
        self.assertNotIn("AIST_VPN_CA_CERT", env)
        self.assertNotIn("AIST_VPN_CLIENT_CERT", env)
        self.assertNotIn("AIST_VPN_TLS_AUTH_KEY", env)


# ---------------------------------------------------------------------------
# Unit tests: _get_own_eth0_ip IP detection
# ---------------------------------------------------------------------------

class GetOwnEth0IpTests(AISTApiBase):
    """Verify _get_own_eth0_ip falls back correctly across detection methods."""

    @patch("socket.gethostbyname", return_value="172.19.0.8")
    @patch("socket.gethostname", return_value="worker")
    def test_socket_method_returns_non_loopback(self, _mock_name, _mock_addr):
        from aist.utils.vpn import _get_own_eth0_ip  # noqa: PLC0415
        self.assertEqual(_get_own_eth0_ip(), "172.19.0.8")

    @patch("socket.gethostbyname", return_value="127.0.0.1")
    @patch("socket.gethostname", return_value="worker")
    @patch("subprocess.run")
    def test_skips_loopback_falls_back_to_ip_command(self, mock_run, _mock_name, _mock_addr):
        """When socket returns 127.x, fall back to 'ip' command."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="  inet 172.19.0.8/16 brd 172.19.255.255\n",
        )
        from aist.utils.vpn import _get_own_eth0_ip  # noqa: PLC0415
        result = _get_own_eth0_ip()
        self.assertEqual(result, "172.19.0.8")

    @patch("socket.gethostbyname", side_effect=OSError("no name"))
    @patch("socket.gethostname", return_value="worker")
    @patch("subprocess.run", side_effect=FileNotFoundError("ip not found"))
    def test_all_methods_fail_returns_none(self, _mock_run, _mock_name, _mock_addr):
        from aist.utils.vpn import _get_own_eth0_ip  # noqa: PLC0415
        result = _get_own_eth0_ip()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Unit tests: vpn_sidecar_context security
# ---------------------------------------------------------------------------

class VpnSidecarContextSecurityTests(AISTApiBase):
    """Verify that vpn_sidecar_context does not leak credentials via exceptions."""

    def _make_resolved(self):
        from aist.integrations.resolver import ResolvedIntegration  # noqa: PLC0415
        secret = SimpleNamespace(
            ovpn_content=_BASE_OVPN,
            ca_cert=_FAKE_CA,
            client_cert="",
            client_key=_FAKE_KEY,
            tls_auth_key="",
            tls_key_type="tls-auth",
            vpn_username="testuser",
            vpn_password="hunter2",
        )
        integration = SimpleNamespace(vpn_secret=secret)
        return ResolvedIntegration(integration=integration, config={})

    @patch("aist.utils.vpn._build_vpn_sidecar_if_needed")
    @patch("subprocess.run")
    def test_docker_run_failure_raises_runtime_error_not_called_process_error(
        self, mock_run, _mock_build,
    ):
        """CalledProcessError must NOT propagate — its .cmd contains credentials."""
        mock_run.side_effect = CalledProcessError(
            1, cmd=["docker", "run", "-e", "AIST_VPN_PASSWORD=hunter2"],
        )
        from aist.utils.vpn import vpn_sidecar_context  # noqa: PLC0415
        resolved = self._make_resolved()
        with self.assertRaises(RuntimeError) as ctx:
            with vpn_sidecar_context(resolved, execution_id="sectest") as _:
                pass  # pragma: no cover
        # The RuntimeError message must NOT contain the credential value
        self.assertNotIn("hunter2", str(ctx.exception))
        self.assertNotIn("AIST_VPN_PASSWORD", str(ctx.exception))

    @patch("aist.utils.vpn._build_vpn_sidecar_if_needed")
    @patch("aist.utils.vpn._wait_for_sidecar_ready")
    @patch("aist.utils.vpn._stop_sidecar")
    @patch("subprocess.run")
    def test_stop_sidecar_called_on_wait_failure(
        self, mock_run, mock_stop, mock_wait, _mock_build,
    ):
        """_stop_sidecar must be called even when _wait_for_sidecar_ready raises."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_wait.side_effect = RuntimeError("tun0 timeout")
        from aist.utils.vpn import vpn_sidecar_context  # noqa: PLC0415
        resolved = self._make_resolved()
        with self.assertRaises(RuntimeError):
            with vpn_sidecar_context(resolved, execution_id="stoptest") as _:
                pass  # pragma: no cover
        mock_stop.assert_called_once()

    @patch("aist.utils.vpn._build_vpn_sidecar_if_needed")
    @patch("aist.utils.vpn._wait_for_sidecar_ready")
    @patch("aist.utils.vpn._stop_sidecar")
    @patch("subprocess.run")
    def test_tls_key_type_and_direction_passed_as_env_not_credentials(
        self, mock_run, mock_stop, _mock_wait, _mock_build,
    ):
        """Non-secret metadata must be in -e args; credential keys must also be present."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        from aist.utils.vpn import vpn_sidecar_context  # noqa: PLC0415
        resolved = self._make_resolved()
        with vpn_sidecar_context(resolved, execution_id="envtest") as _:
            pass
        # Find the docker run call
        run_calls = [c for c in mock_run.call_args_list if c.args and "run" in (c.args[0] or [])]
        self.assertTrue(run_calls, "docker run was not called")
        cmd = run_calls[0].args[0]
        cmd_str = " ".join(str(x) for x in cmd)
        # Non-secret metadata must be present
        self.assertIn("AIST_VPN_TLS_KEY_TYPE", cmd_str)
        self.assertIn("AIST_VPN_TLS_KEY_DIRECTION", cmd_str)
        # Credentials also present (still via -e, known limitation)
        self.assertIn("AIST_VPN_OVPN_CONTENT", cmd_str)

    @patch("aist.utils.vpn._build_vpn_sidecar_if_needed")
    @patch("aist.utils.vpn._wait_for_sidecar_ready")
    @patch("aist.utils.vpn._stop_sidecar")
    @patch("aist.utils.vpn._get_own_eth0_ip", return_value="172.19.0.8")
    @patch("subprocess.run")
    def test_allowed_ip_passed_to_sidecar_when_eth0_detected(
        self, mock_run, _mock_ip, mock_stop, _mock_wait, _mock_build,
    ):
        """AIST_ALLOWED_IP must be included in docker run when own IP is detected."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        from aist.utils.vpn import vpn_sidecar_context  # noqa: PLC0415
        resolved = self._make_resolved()
        with vpn_sidecar_context(resolved, execution_id="iptest") as _:
            pass
        run_calls = [c for c in mock_run.call_args_list if c.args and "run" in (c.args[0] or [])]
        self.assertTrue(run_calls, "docker run was not called")
        cmd = run_calls[0].args[0]
        cmd_str = " ".join(str(x) for x in cmd)
        self.assertIn("AIST_ALLOWED_IP=172.19.0.8", cmd_str)

    @patch("aist.utils.vpn._build_vpn_sidecar_if_needed")
    @patch("aist.utils.vpn._wait_for_sidecar_ready")
    @patch("aist.utils.vpn._stop_sidecar")
    @patch("aist.utils.vpn._get_own_eth0_ip", return_value=None)
    @patch("subprocess.run")
    def test_no_allowed_ip_when_eth0_not_detected(
        self, mock_run, _mock_ip, mock_stop, _mock_wait, _mock_build,
    ):
        """AIST_ALLOWED_IP must not be added when own IP cannot be detected."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        from aist.utils.vpn import vpn_sidecar_context  # noqa: PLC0415
        resolved = self._make_resolved()
        with vpn_sidecar_context(resolved, execution_id="noitest") as _:
            pass
        run_calls = [c for c in mock_run.call_args_list if c.args and "run" in (c.args[0] or [])]
        self.assertTrue(run_calls, "docker run was not called")
        cmd = run_calls[0].args[0]
        cmd_str = " ".join(str(x) for x in cmd)
        self.assertNotIn("AIST_ALLOWED_IP", cmd_str)


# ---------------------------------------------------------------------------
# Unit tests: orphaned container cleanup
# ---------------------------------------------------------------------------

class OrphanCleanupTests(AISTApiBase):

    def _make_mock_run(self, containers: list[dict]):
        lines = "\n".join(json.dumps(c) for c in containers)
        return MagicMock(returncode=0, stdout=lines + "\n" if lines else "")

    @patch("subprocess.run")
    def test_removes_containers_older_than_max_age(self, mock_run):
        old_ts = "2020-01-01 00:00:00 +0000 UTC"
        mock_run.return_value = self._make_mock_run([
            {"Names": "aist-vpn-old", "CreatedAt": old_ts},
        ])
        removed = cleanup_orphaned_vpn_containers(max_age_minutes=30)
        self.assertEqual(removed, 1)

    @patch("subprocess.run")
    def test_skips_recent_containers(self, mock_run):
        recent = (datetime.now(tz=timezone.utc) - timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M:%S +0000 UTC"
        )
        mock_run.return_value = self._make_mock_run([
            {"Names": "aist-vpn-recent", "CreatedAt": recent},
        ])
        removed = cleanup_orphaned_vpn_containers(max_age_minutes=30)
        self.assertEqual(removed, 0)

    @patch("subprocess.run")
    def test_non_utc_timezone_parsed_correctly(self, mock_run):
        """Container created 6h ago in +0200 zone must be detected as old (>30 min)."""
        # 6 hours ago in UTC, expressed as +0200
        base_utc = datetime.now(tz=timezone.utc) - timedelta(hours=6)
        local_dt = base_utc.astimezone(timezone(timedelta(hours=2)))
        ts = local_dt.strftime("%Y-%m-%d %H:%M:%S +0200 CEST")
        mock_run.return_value = self._make_mock_run([
            {"Names": "aist-vpn-tz", "CreatedAt": ts},
        ])
        removed = cleanup_orphaned_vpn_containers(max_age_minutes=30)
        self.assertEqual(removed, 1)

    @patch("subprocess.run")
    def test_unparseable_created_at_skipped(self, mock_run):
        mock_run.return_value = self._make_mock_run([
            {"Names": "aist-vpn-bad", "CreatedAt": "not-a-date"},
        ])
        # Must not raise, must skip silently
        removed = cleanup_orphaned_vpn_containers(max_age_minutes=30)
        self.assertEqual(removed, 0)

    @patch("subprocess.run")
    def test_empty_output_returns_zero(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        self.assertEqual(cleanup_orphaned_vpn_containers(), 0)


# ---------------------------------------------------------------------------
# API / serializer tests: ovpn upload triggers PEM extraction
# ---------------------------------------------------------------------------

class VpnSecretSerializerParseTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.org_pt = Product_Type.objects.create(name="VPN Test PT")
        self.org = Organization.objects.create(name="VPN Org", product_type=self.org_pt)
        Product_Type_Member.objects.create(
            product_type=self.org_pt,
            user=self.user,
            role=self.role_maintainer,
        )
        self.project.organization = self.org
        self.project.save(update_fields=["organization"])
        self.list_url = reverse("aist_api:org_integration_list_create", kwargs={"org_id": self.org.pk})

    def _create_vpn_integration(self, ovpn_content):
        return self.client.post(self.list_url, {
            "integration_type": "VPN",
            "name": "Corp VPN",
            "config": {},
            "vpn_secret": {"ovpn_content": ovpn_content},
        }, format="json")

    def test_upload_ovpn_with_inline_certs_strips_blocks(self):
        resp = self._create_vpn_integration(_FULL_OVPN_WITH_CERTS)
        self.assertEqual(resp.status_code, 201)

        from aist.models import OrgIntegrationVPNSecret  # noqa: PLC0415
        secret = OrgIntegrationVPNSecret.objects.get()
        self.assertIn("FAKECA", secret.ca_cert)
        self.assertIn("FAKECERT", secret.client_cert)
        self.assertIn("FAKEKEY", secret.client_key)
        self.assertNotIn("<ca>", secret.ovpn_content)
        self.assertNotIn("<cert>", secret.ovpn_content)
        self.assertNotIn("<key>", secret.ovpn_content)
        self.assertIn("remote vpn.example.com 1194", secret.ovpn_content)

    def test_upload_clean_ovpn_leaves_cert_fields_empty(self):
        resp = self._create_vpn_integration(_BASE_OVPN)
        self.assertEqual(resp.status_code, 201)

        from aist.models import OrgIntegrationVPNSecret  # noqa: PLC0415
        secret = OrgIntegrationVPNSecret.objects.get()
        self.assertEqual(secret.ca_cert, "")
        self.assertEqual(secret.client_cert, "")
        self.assertEqual(secret.client_key, "")

    def test_response_has_presence_indicators_not_raw_certs(self):
        resp = self._create_vpn_integration(_FULL_OVPN_WITH_CERTS)
        self.assertEqual(resp.status_code, 201)
        vpn_secret_data = resp.data.get("vpn_secret", {})
        self.assertTrue(vpn_secret_data["has_ovpn_content"])
        self.assertTrue(vpn_secret_data["has_client_cert"])
        self.assertTrue(vpn_secret_data["has_client_key"])
        for field in ("ovpn_content", "ca_cert", "client_cert", "client_key", "tls_auth_key"):
            self.assertNotIn(field, vpn_secret_data)

    def test_explicit_ca_cert_not_overwritten_by_parsed_value(self):
        """When ca_cert is explicitly provided, it takes precedence over the inline block."""
        explicit_ca = "-----BEGIN CERTIFICATE-----\nEXPLICIT\n-----END CERTIFICATE-----"
        resp = self.client.post(self.list_url, {
            "integration_type": "VPN",
            "name": "Corp VPN2",
            "config": {},
            "vpn_secret": {
                "ovpn_content": _FULL_OVPN_WITH_CERTS,
                "ca_cert": explicit_ca,
            },
        }, format="json")
        self.assertEqual(resp.status_code, 201)

        from aist.models import OrgIntegrationVPNSecret  # noqa: PLC0415
        secret = OrgIntegrationVPNSecret.objects.get()
        self.assertIn("EXPLICIT", secret.ca_cert)
        self.assertNotIn("FAKECA", secret.ca_cert)

    def test_vpn_secret_not_shown_for_non_vpn_integration(self):
        resp = self.client.post(self.list_url, {
            "integration_type": "GITLAB",
            "name": "GitLab",
            "config": {},
            "secret": "token",
        }, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn("vpn_secret", resp.data)

    def test_upload_ovpn_with_tls_crypt_v2(self):
        """tls-crypt-v2 block must be extracted and tls_key_type set correctly."""
        ovpn = _BASE_OVPN + f"<tls-crypt-v2>\n{_FAKE_TLS_AUTH}\n</tls-crypt-v2>\n"
        resp = self._create_vpn_integration(ovpn)
        self.assertEqual(resp.status_code, 201)

        from aist.models import OrgIntegrationVPNSecret  # noqa: PLC0415
        secret = OrgIntegrationVPNSecret.objects.get()
        self.assertIn("FAKETLS", secret.tls_auth_key)
        self.assertEqual(secret.tls_key_type, "tls-crypt-v2")
        self.assertNotIn("<tls-crypt-v2>", secret.ovpn_content)
