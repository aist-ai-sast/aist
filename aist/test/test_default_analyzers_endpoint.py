"""
Admin UI auto-discovery sanity test for new agent-bridge analyzers.

Adding a YAML entry to ``sast-combinator/sast-pipeline/pipeline/config/analyzers.yaml``
must be enough to surface the analyzer in:

- the ``AISTPipelineRunForm`` checkbox list (driven by
  ``cfg.get_supported_analyzers()`` - see ``aist/forms.py`` lines 147-151);
- the JS-side default-analyzers picker (driven by
  ``default_analyzers_payload`` - see ``aist/api/projects.py`` lines 510-526).

This test pins that auto-discovery so a future refactor cannot regress it
for new agent analyzers - without requiring template changes for each
new analyzer.
"""
from __future__ import annotations

from django.urls import reverse

from aist.api.projects import default_analyzers_payload
from aist.models import Organization
from aist.pipeline_args import PipelineArguments
from aist.test.analyzer_catalog import SyntheticAnalyzerCatalogMixin
from aist.test.test_api import AISTApiBase
from aist.utils.pipeline_imports import _load_analyzers_config


class FullAnalyzerAutoDiscoveryTests(AISTApiBase):

    def test_full_analyzer_appears_in_supported_list(self):
        # Drives the form's checkbox choices in _pipeline_run_form.html.
        cfg = _load_analyzers_config()
        supported = list(cfg.get_supported_analyzers())
        self.assertIn(
            "claude-full-security",
            supported,
            f"claude-full-security missing from supported analyzers: {supported}",
        )

    def test_full_analyzer_appears_in_default_payload_for_supported_language(self):
        # Drives the JS default-pre-check behavior in _pipeline_run_js.html.
        # claude-full-security declares python (among others) and time_class slow,
        # so it must surface for that combination.
        payload = default_analyzers_payload(
            project=None,
            project_id=None,
            langs=["python"],
            time_class="slow",
        )
        self.assertIn(
            "claude-full-security",
            payload["defaults"],
            f"claude-full-security missing from defaults: {payload['defaults']}",
        )

    def test_diff_and_full_coexist_in_default_payload(self):
        # Both agent analyzers must appear together — Task 7 (no mutex) is
        # what allows this; the test fails the day someone adds a hidden
        # mutex back into get_filtered_analyzers.
        payload = default_analyzers_payload(
            project=None,
            project_id=None,
            langs=["python"],
            time_class="slow",
        )
        self.assertIn("claude-diff-security", payload["defaults"])
        self.assertIn("claude-full-security", payload["defaults"])

    def test_full_analyzer_skipped_for_unsupported_language(self):
        # Analyzer must respect language filtering. Pick a language not
        # in the YAML language list.
        payload = default_analyzers_payload(
            project=None,
            project_id=None,
            langs=["unsupported-lang-xyz"],
            time_class="slow",
        )
        self.assertNotIn("claude-full-security", payload["defaults"])


class DefaultAnalyzersMatchTheLaunchTests(SyntheticAnalyzerCatalogMixin, AISTApiBase):

    """The analyzers the run form pre-selects are the ones a launch naming none runs, profile included."""

    CATALOG = {
        "analyzers": [
            {"name": "quick", "image": "q", "language": ["python"], "time_class": "fast"},
            {"name": "noisy", "image": "n", "language": ["python"], "time_class": "fast"},
            {"name": "deep", "image": "d", "language": ["python"], "time_class": "slow"},
        ],
    }

    def setUp(self):
        super().setUp()
        Organization.objects.create(name="Default analyzers organization", product_type=self.prod_type)

    def test_the_form_defaults_and_a_launch_apply_the_same_ceiling_and_profile(self):
        self.project.profile = {"analyzers": {"exclude": ["noisy"], "include": ["deep"]}}
        self.project.save(update_fields=["profile"])

        resp = self.client.post(
            reverse("aist_api:default_analyzers"),
            data={"project": self.project.id, "time_class_level": "fast", "languages": []},
            format="json",
        )
        launched = PipelineArguments.for_sast(
            project=self.project, raw_params={"time_class_level": "fast"},
        ).sast.analyzers

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["defaults"], ["quick", "deep"])
        self.assertEqual(launched, ["quick", "deep"])
