# ruff: noqa: EM101, EM102, S106, TRY003
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "dast-staging-canary.py"
SPEC = importlib.util.spec_from_file_location("dast_staging_canary", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load DAST staging canary script")
CANARY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CANARY
SPEC.loader.exec_module(CANARY)


class FakeCanaryClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path.endswith("dast-integration/import/"):
            return {"id": 41, "secret": "must-not-be-recorded"}
        if path == "integrations/41/validate/":
            return {"task_id": "validation-41"}
        if path == "integrations/41/validate/validation-41/":
            return {"state": "READY", "valid": True, "detail": ""}
        if path == "dast-integrations/41/sync-capabilities/":
            return {"task_id": "sync-41"}
        if path == "dast-integrations/41/onboarding/":
            return {
                "dast_state": {
                    "validation_state": "READY",
                    "contract_version": "2.0",
                    "capabilities_synced_at": "2026-07-26T10:00:00Z",
                    "sync_error_code": "",
                },
            }
        if path.endswith("/dast-targets/"):
            return [{"provider_id": "safe-clean"}, {"provider_id": "safe-findings"}]
        if path.endswith("/start/"):
            return {"id": 77, "pipeline_id": None, "queued": True}
        if path == "launch-requests/?limit=2000":
            return {"results": [{"id": 77, "state": "DISPATCHED", "pipeline_id": "pipe-77"}]}
        if path == "pipelines/pipe-77":
            return {
                "status": "FINISHED",
                "dast_outcome_code": "SUCCESS_CLEAN",
                "external_run_id": "run-77",
            }
        raise AssertionError(f"Unexpected fake request: {method} {path}")

    def get_text(self, path):
        self.calls.append(("GET_TEXT", path, None))
        return b"bounded live log\n"


class DastStagingCanaryTests(SimpleTestCase):
    def _json_file(self, directory, name, value):
        path = Path(directory) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_onboarding_uses_bundle_vpn_validation_and_sync_but_returns_only_safe_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self._json_file(directory, "bundle.json", {"token": "provider-secret"})
            args = SimpleNamespace(
                bundle=bundle,
                organization_id=3,
                name="Approved gateway",
                vpn_integration_id=9,
                timeout=1,
                poll_interval=0,
            )
            client = FakeCanaryClient()

            evidence = CANARY.onboard(client, args)

        self.assertEqual(evidence["integration_id"], 41)
        self.assertEqual(evidence["contract_version"], "2.0")
        self.assertEqual(evidence["target_ids"], ["safe-clean", "safe-findings"])
        self.assertNotIn("provider-secret", json.dumps(evidence))
        imported_payload = client.calls[0][2]
        self.assertEqual(imported_payload["vpn_integration_id"], 9)
        self.assertEqual(imported_payload["bundle"]["token"], "provider-secret")

    def test_run_canary_records_hashes_not_logs_and_requires_matching_provider_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self._json_file(
                directory,
                "manifest.json",
                {
                    "version": 1,
                    "approval_ref": "SEC-2041",
                    "project_id": 12,
                    "cases": [
                        {
                            "name": "exact clean",
                            "launch_config_id": 31,
                            "project_version_id": 44,
                            "expected_outcome": "SUCCESS_CLEAN",
                            "expected_relation": "exact",
                            "expected_distance": 0,
                            "request_stop": False,
                        },
                    ],
                },
            )
            provider_evidence = self._json_file(
                directory,
                "provider.json",
                {
                    "version": 1,
                    "direct_non_vpn_blocked": True,
                    "runs": [
                        {
                            "correlation_id": "pipe-77",
                            "run_id": "run-77",
                            "relation": "exact",
                            "distance": 0,
                        },
                    ],
                },
            )
            args = SimpleNamespace(
                manifest=manifest,
                timeout=1,
                poll_interval=0,
            )

            evidence = CANARY.run_canary(FakeCanaryClient(), args)
            aist_evidence = self._json_file(directory, "aist.json", evidence)
            verified = CANARY.verify_canary(
                SimpleNamespace(aist_evidence=aist_evidence, provider_evidence=provider_evidence),
            )

        case = evidence["cases"][0]
        self.assertEqual(case["pipeline_id"], "pipe-77")
        self.assertEqual(case["logs_bytes"], len(b"bounded live log\n"))
        self.assertEqual(len(case["logs_sha256"]), 64)
        self.assertNotIn("bounded live log", json.dumps(evidence))
        self.assertFalse(evidence["provider_verified"])
        self.assertTrue(verified["direct_non_vpn_blocked"])
        self.assertTrue(verified["provider_verified"])

    def test_manifest_and_provider_evidence_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self._json_file(
                directory,
                "manifest.json",
                {
                    "version": 1,
                    "approval_ref": "SEC-2041",
                    "project_id": 12,
                    "cases": [
                        {
                            "name": "invalid exact",
                            "launch_config_id": 31,
                            "project_version_id": 44,
                            "expected_outcome": "SUCCESS_CLEAN",
                            "expected_relation": "exact",
                            "expected_distance": 1,
                            "request_stop": False,
                        },
                    ],
                },
            )
            with self.assertRaisesMessage(CANARY.CanaryError, "distance 0"):
                CANARY._load_run_manifest(manifest)

            provider = self._json_file(
                directory,
                "provider.json",
                {"version": 1, "direct_non_vpn_blocked": False, "runs": []},
            )
            with self.assertRaisesMessage(CANARY.CanaryError, "direct non-VPN"):
                CANARY._verify_provider_evidence([], provider)

    def test_client_rejects_non_tls_base_url_and_blank_token(self):
        with self.assertRaisesMessage(CANARY.CanaryError, "HTTPS"):
            CANARY.AistClient(base_url="http://aist.example", token="token")
        with self.assertRaisesMessage(CANARY.CanaryError, "AIST_CANARY_TOKEN"):
            CANARY.AistClient(base_url="https://aist.example", token="")
