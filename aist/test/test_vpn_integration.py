"""
Tests for VPN integration: OVPN PEM-block parser, serializer validation,
and _assemble_env base64 encoding.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import patch

from django.urls import reverse
from dojo.models import Product_Type, Product_Type_Member

from aist.api.org_integrations import _split_ovpn_pem_blocks
from aist.models import OrgIntegration, OrgIntegrationType, Organization
from aist.test.test_api import AISTApiBase
from aist.utils.vpn import _assemble_env

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
        # cleaned config should have no PEM tags
        for tag in ("<ca>", "<cert>", "<key>"):
            self.assertNotIn(tag, cleaned)
        # but connection directives remain
        self.assertIn("remote vpn.example.com 1194", cleaned)

    def test_excessive_blank_lines_collapsed(self):
        ovpn = f"client\n\n\n\n<ca>\n{_FAKE_CA}\n</ca>\n\n\n"
        cleaned, _ = _split_ovpn_pem_blocks(ovpn)
        self.assertNotIn("\n\n\n", cleaned)


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
        # Certs should be in separate fields
        self.assertIn("FAKECA", secret.ca_cert)
        self.assertIn("FAKECERT", secret.client_cert)
        self.assertIn("FAKEKEY", secret.client_key)
        # ovpn_content should only have directives, no PEM tags
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
        # raw values must never appear in response
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
