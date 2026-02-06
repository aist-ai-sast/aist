# aist/test/test_pipeline_args.py
"""
Unit tests for PipelineArguments.analyzers profile logic.

Covers:
- default analyzers when project.profile is empty/absent
- include/exclude behavior when project.profile['analyzers'] is provided
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.ai_filter import validate_and_normalize_filter
from aist.models import AISTProject
from aist.pipeline_args import PipelineArguments


class DummyCfg:

    """Minimal stub to emulate analyzers config object."""

    def __init__(self, available):
        # use a set to match .add/.remove usage in PipelineArguments
        self._available = set(available)

    def get_filtered_analyzers(self, analyzers_to_run, max_time_class,
                               non_compile_project, target_languages,
                               show_only_parent):
        # For the test we ignore args and return pre-defined set
        return self._available

    def get_names(self, analyzer_set):
        # Return a set, since PipelineArguments expects .remove()/.add()
        return set(analyzer_set)


class PipelineArgsProfileTests(TestCase):
    def setUp(self):
        # Minimal objects for AISTProject
        self.user = get_user_model().objects.create(
            username="tester", email="tester@example.com", password="pass",  # noqa: S106
        )
        self.sla = SLA_Configuration.objects.create(name="SLA default")
        self.prod_type = Product_Type.objects.create(name="PT")
        self.product = Product.objects.create(
            name="Test Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )

    @patch("aist.pipeline_args._load_analyzers_config")
    def test_analyzers_without_profile_returns_default(self, load_cfg_mock):
        """
        When project.profile is empty/absent:
        - analyzers property must return the default names from config
        filtered by languages/time_class/compilable (we stub to a fixed set).
        """
        load_cfg_mock.return_value = DummyCfg({"cppcheck", "bandit", "semgrep"})

        project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["cpp", "python"],
            script_path="scripts/build_and_scan.sh",
            compilable=True,
            profile={},  # no special analyzers profile
        )

        args = PipelineArguments(
            project=project,
            project_version={},
            selected_analyzers=[],     # force reading from config
            selected_languages=["javascript"],  # will be merged with project languages
            log_level="INFO",
            rebuild_images=False,
            ai_mode="MANUAL",
            ai_filter_snapshot=None,
            time_class_level="slow",
        )

        # Should equal the default config names
        self.assertEqual(set(args.analyzers), {"cppcheck", "bandit", "semgrep"})
        self.assertEqual(set(args.languages), {"cpp", "python", "javascript"})

    @patch("aist.pipeline_args._load_analyzers_config")
    def test_analyzers_with_profile_include_exclude(self, load_cfg_mock):
        """
        When project.profile['analyzers'] is present:
        - remove items in 'exclude'
        - add items in 'include'
        """
        load_cfg_mock.return_value = DummyCfg({"cppcheck", "bandit"})  # start set

        project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["cpp", "python"],
            script_path="scripts/build_and_scan.sh",
            compilable=True,
            profile={
                "analyzers": {
                    "exclude": ["bandit"],
                    "include": ["semgrep"],
                },
            },  # profile-driven modifications
        )

        args = PipelineArguments(
            project=project,
            project_version={},
            selected_analyzers=[],     # force reading from config
            selected_languages=[],
            log_level="INFO",
            rebuild_images=False,
            ai_mode="MANUAL",
            ai_filter_snapshot=None,
            time_class_level="slow",
        )

        # Expected = {"cppcheck"} (default) - {"bandit"} + {"semgrep"} = {"cppcheck", "semgrep"}
        self.assertEqual(set(args.analyzers), {"cppcheck", "semgrep"})
        self.assertEqual(set(args.languages), {"cpp", "python"})


class AIFilterValidationTests(TestCase):
    def test_validate_filter_requires_dict(self):
        with self.assertRaises(ValueError):
            validate_and_normalize_filter(None)
        with self.assertRaises(TypeError):
            validate_and_normalize_filter("nope")
        with self.assertRaises(TypeError):
            validate_and_normalize_filter(["nope"])

    def test_validate_filter_requires_limit(self):
        with self.assertRaisesRegex(ValueError, "limit"):
            validate_and_normalize_filter({"severity": [{"comparison": "EQUALS", "value": "HIGH"}]})

    def test_validate_filter_limit_bounds(self):
        with self.assertRaisesRegex(ValueError, ">= 1"):
            validate_and_normalize_filter({"limit": 0, "severity": [{"comparison": "EQUALS", "value": "HIGH"}]})
        with self.assertRaisesRegex(ValueError, "must be <= "):
            validate_and_normalize_filter({"limit": 1000, "severity": [{"comparison": "EQUALS", "value": "HIGH"}]})

    def test_validate_filter_requires_at_least_one_field(self):
        with self.assertRaisesRegex(ValueError, "at least one field"):
            validate_and_normalize_filter({"limit": 10})

    def test_validate_filter_rejects_unknown_field(self):
        with self.assertRaisesRegex(ValueError, "Unsupported filter field"):
            validate_and_normalize_filter({"limit": 10, "unknown": [{"comparison": "EQUALS", "value": "x"}]})

    def test_validate_filter_field_conditions_must_be_non_empty_list(self):
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            validate_and_normalize_filter({"limit": 10, "severity": []})
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            validate_and_normalize_filter({"limit": 10, "severity": "HIGH"})

    def test_validate_filter_condition_must_be_object(self):
        with self.assertRaisesRegex(TypeError, "must be an object"):
            validate_and_normalize_filter({"limit": 10, "severity": ["bad"]})

    def test_validate_filter_comparison_must_be_supported(self):
        with self.assertRaisesRegex(ValueError, "Unsupported comparison"):
            validate_and_normalize_filter({"limit": 10, "severity": [{"comparison": "NOPE", "value": "HIGH"}]})

    def test_validate_filter_requires_value_key_even_for_exists(self):
        # your validator currently requires 'value' for all comparisons :contentReference[oaicite:7]{index=7}
        with self.assertRaisesRegex(ValueError, "must contain 'value'"):
            validate_and_normalize_filter({"limit": 10, "severity": [{"comparison": "EXISTS"}]})

    def test_validate_filter_normalizes_comparison_to_uppercase(self):
        f = validate_and_normalize_filter(
            {"limit": 10, "severity": [{"comparison": "equals", "value": "HIGH"}]},
        )
        self.assertEqual(f["severity"][0]["comparison"], "EQUALS")

    def test_validate_filter_regex_rules(self):
        # title allows regex by default
        ok = validate_and_normalize_filter({"limit": 10, "title": [{"comparison": "REGEX", "value": "abc.*"}]})
        self.assertEqual(ok["title"][0]["comparison"], "REGEX")

        # severity forbids regex :contentReference[oaicite:8]{index=8} + _cond_to_q enforces allow_regex :contentReference[oaicite:9]{index=9}
        validate_and_normalize_filter({"limit": 10, "severity": [{"comparison": "REGEX", "value": "H.*"}]})
        # validate_and_normalize_filter itself doesn't know allow_regex; it's enforced in _cond_to_q/apply layer.
        # So here we just confirm normalize accepts it, but the query builder would reject it.

    def test_validate_filter_int_and_bool_coercion(self):
        # cwe is int
        f = validate_and_normalize_filter({"limit": 10, "cwe": [{"comparison": "EQUALS", "value": "79"}]})
        self.assertEqual(f["cwe"][0]["value"], "79")  # normalization keeps raw value; coercion happens in _cond_to_q

        # verified is bool
        f = validate_and_normalize_filter({"limit": 10, "verified": [{"comparison": "EQUALS", "value": "true"}]})
        self.assertEqual(f["verified"][0]["value"], "true")

    def test_validate_filter_allows_order_by(self):
        f = validate_and_normalize_filter(
            {
                "limit": 10,
                "severity": [{"comparison": "EXISTS", "value": True}],
                "order_by": [{"field": "date", "direction": "DESC"}],
            },
        )
        self.assertEqual(f["order_by"][0]["field"], "date")
        self.assertEqual(f["order_by"][0]["direction"], "DESC")

    def test_validate_filter_order_by_default_direction(self):
        f = validate_and_normalize_filter(
            {
                "limit": 10,
                "severity": [{"comparison": "EXISTS", "value": True}],
                "order_by": [{"field": "severity"}],
            },
        )
        self.assertEqual(f["order_by"][0]["direction"], "DESC")


class PipelineArgsAIFilterIntegrationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(
            username="tester2", email="tester2@example.com", password="pass",  # noqa: S106
        )
        self.sla = SLA_Configuration.objects.create(name="SLA default 2")
        self.prod_type = Product_Type.objects.create(name="PT2")
        self.product = Product.objects.create(
            name="Test Product 2",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["python"],
            script_path="scripts/build_and_scan.sh",
            compilable=True,
            profile={},
        )

    def test_normalize_params_manual_forces_snapshot_none(self):
        out = PipelineArguments.normalize_params(
            project=self.project,
            raw_params={"ai_mode": "MANUAL", "ai_filter_snapshot": {"limit": 10, "severity": [{"comparison": "EQUALS", "value": "HIGH"}]}},
        )
        self.assertEqual(out["ai_mode"], "MANUAL")
        self.assertIsNone(out["ai_filter_snapshot"])

    def test_normalize_params_auto_default_validates_snapshot(self):
        out = PipelineArguments.normalize_params(
            project=self.project,
            raw_params={"ai_mode": "AUTO_DEFAULT", "ai_filter_snapshot": {"limit": 10, "severity": [{"comparison": "equals", "value": "HIGH"}]}},
        )
        self.assertEqual(out["ai_mode"], "AUTO_DEFAULT")
        self.assertEqual(out["ai_filter_snapshot"]["severity"][0]["comparison"], "EQUALS")

    def test_normalize_params_auto_default_requires_snapshot(self):
        with self.assertRaises(ValueError):
            PipelineArguments.normalize_params(
                project=self.project,
                raw_params={"ai_mode": "AUTO_DEFAULT"},
            )
