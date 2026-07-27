import json

from django.test import SimpleTestCase

from aist.api.org_integrations import DastOnboardingBundleSerializer
from aist.integrations.dast_config import DastConfigError, DastOnboardingBundle

TOKEN = "pub_aist.secretvaluevaluevalue"  # noqa: S105


def _bundle(**overrides):
    payload = {
        "bundle_version": 1,
        "gateway_url": "https://dast-gateway.internal/",
        "ca_bundle": "-----BEGIN CERTIFICATE-----\npublic-ca\n-----END CERTIFICATE-----",
        "contract_major": 2,
        "integrator_public_id": "pub_aist",
        "server_fingerprint": "sha256:server-fingerprint",
        "token": TOKEN,
    }
    payload.update(overrides)
    return payload


class DastOnboardingBundleTests(SimpleTestCase):
    def test_valid_json_is_canonicalized_without_exposing_token(self):
        bundle = DastOnboardingBundle.from_json(json.dumps(_bundle()))

        self.assertEqual(bundle.config.gateway_url, "https://dast-gateway.internal")
        self.assertEqual(bundle.token, TOKEN)
        self.assertNotIn("token", bundle.to_safe_snapshot())
        self.assertNotIn(TOKEN, repr(bundle))

    def test_rejects_unknown_missing_and_unsupported_versions(self):
        cases = (
            {**_bundle(), "legacy_url": "https://legacy.invalid"},
            {key: value for key, value in _bundle().items() if key != "server_fingerprint"},
            _bundle(bundle_version=0),
            _bundle(bundle_version=2),
            _bundle(contract_major=1),
        )

        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(DastConfigError):
                DastOnboardingBundle.from_mapping(payload)

    def test_rejects_malicious_url_pem_and_token_values(self):
        cases = (
            _bundle(gateway_url="http://dast-gateway.internal"),
            _bundle(gateway_url="https://user:password@dast-gateway.internal"),
            _bundle(gateway_url="https://dast-gateway.internal?token=leak"),
            _bundle(ca_bundle="-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"),
            _bundle(ca_bundle="not a public certificate"),
            _bundle(token=f" {TOKEN} "),
            _bundle(token="x" * (DastOnboardingBundle.MAX_TOKEN_BYTES + 1)),
        )

        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(DastConfigError):
                DastOnboardingBundle.from_mapping(payload)

    def test_rejects_oversize_ca_and_json(self):
        with self.assertRaises(DastConfigError):
            DastOnboardingBundle.from_mapping(
                _bundle(ca_bundle="x" * (DastOnboardingBundle.MAX_CA_BUNDLE_BYTES + 1)),
            )
        with self.assertRaises(DastConfigError):
            DastOnboardingBundle.from_json(b" " * (DastOnboardingBundle.MAX_BUNDLE_BYTES + 1))

    def test_rejects_duplicate_fields_malformed_utf8_and_non_object_json(self):
        invalid_inputs = (
            '{"bundle_version": 1, "bundle_version": 1}',
            b"\xff",
            "[]",
            "not-json",
        )

        for raw in invalid_inputs:
            with self.subTest(raw=raw), self.assertRaises(DastConfigError):
                DastOnboardingBundle.from_json(raw)

    def test_serializer_keeps_token_write_only_and_rejects_unknown_fields(self):
        serializer = DastOnboardingBundleSerializer(data=_bundle())

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["token"], TOKEN)
        self.assertNotIn("token", serializer.data)
        self.assertNotIn(TOKEN, str(serializer.data))
        self.assertNotIn(TOKEN, repr(serializer))

        invalid = DastOnboardingBundleSerializer(data={**_bundle(), "fallback_url": "https://legacy.invalid"})
        self.assertFalse(invalid.is_valid())
