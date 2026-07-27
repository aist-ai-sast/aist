import json
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from aist.integrations.dast_config import DastTargetSnapshot
from aist.integrations.dast_gateway_client import DastGatewayClient


class DastOnboardingContractCompatibilityTests(SimpleTestCase):
    def test_pinned_provider_contract_matches_strict_onboarding_client(self):
        contract_path = Path(settings.AIST_PIPELINE_CODE_PATH) / "contracts" / "dast-integration.openapi.json"
        snapshot = json.loads(contract_path.read_text(encoding="utf-8"))
        schemas = snapshot["components"]["schemas"]

        self.assertEqual(
            set(schemas["V2PingResponseSchema"]["properties"]),
            set(DastGatewayClient._PING_FIELDS),
        )
        self.assertEqual(
            set(schemas["V2TargetsResponseSchema"]["properties"]),
            set(DastGatewayClient._CATALOG_FIELDS),
        )
        self.assertEqual(
            set(schemas["V2TargetSchema"]["properties"]),
            set(DastTargetSnapshot._FIELDS),
        )
        self.assertEqual(
            snapshot["paths"][DastGatewayClient.PING_PATH]["get"]["security"],
            [{"BearerAuth": []}],
        )
        self.assertEqual(
            snapshot["paths"][DastGatewayClient.TARGETS_PATH]["get"]["security"],
            [{"BearerAuth": []}],
        )
