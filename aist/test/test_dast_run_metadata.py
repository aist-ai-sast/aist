"""Reading the DAST run metadata: derivations, mixed-type lists, and tenant scoping."""

from __future__ import annotations

from datetime import UTC, datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from aist.integrations.dast_report import (
    DastCoverage,
    DastRunEconomy,
    DastTokenBucket,
    DastTokenUsage,
    ValidatedDastReport,
    ValidatedDastRunMetadata,
)
from aist.models import AISTPipeline, AISTStatus, DastRunMetadata, PipelineExecutionType
from aist.services.dast_run_metadata import dast_run_detail, dast_run_summary, reported_dast_run_preview
from aist.test.test_api import AISTApiBase

# Real values from run 80c744a2be37d91c07a7a8ef97c520be.
COVERAGE = DastCoverage(
    unit="endpoint",
    discovered=784,
    reachable=176,
    analysed=38,
    planned=10,
    analysed_names=("analytics3-test-hdw-mx", "auth-test-hdw-mx", "cloud-prod-hdw-mx"),
    beyond_plan_names=("cloud-prod-hdw-mx",),
)
TOTAL = DastTokenBucket(
    input_tokens=2234,
    output_tokens=951808,
    thinking_tokens=331554,
    cache_creation_tokens=2578204,
    cache_read_tokens=90024238,
    calls=1117,
)
BY_PHASE = (
    DastTokenBucket(
        key="6",
        name="depth: floor, explore, discovery",
        input_tokens=1320,
        output_tokens=584186,
        thinking_tokens=199539,
        cache_creation_tokens=1647095,
        cache_read_tokens=54591759,
        calls=660,
    ),
    DastTokenBucket(
        key="4",
        input_tokens=28,
        output_tokens=6819,
        thinking_tokens=2564,
        cache_creation_tokens=40321,
        cache_read_tokens=2186768,
        calls=14,
    ),
)
BY_AGENT = (
    DastTokenBucket(
        key="dast-check-runner",
        agents=14,
        input_tokens=1450,
        output_tokens=549193,
        thinking_tokens=194712,
        cache_creation_tokens=1626800,
        cache_read_tokens=46481643,
        calls=725,
    ),
    DastTokenBucket(
        key="orchestrator",
        agents=8,
        input_tokens=216,
        output_tokens=109099,
        thinking_tokens=23438,
        cache_creation_tokens=236421,
        cache_read_tokens=22374724,
        calls=108,
    ),
)
GRAND_TOTAL = 2234 + 951808 + 2578204 + 90024238
ECONOMY = DastRunEconomy(
    tokens_per_check=498969,
    tokens=104284586,
    checks=209,
    commands=5629,
    waits=55,
    artifact_reads=995,
    contract_reads=0,
)


def _metadata(**overrides) -> ValidatedDastRunMetadata:
    values = {
        "run_id": "80c744a2be37d91c07a7a8ef97c520be",
        "target_id": "perimeter",
        "stand_id": "external-10host",
        "product_family": "perimeter",
        "tier": "external",
        "run_type": "deep",
        "target_host": "analytics3.test.hdw.mx",
        "scan_started": datetime(2026, 8, 17, 17, 37, 46, tzinfo=UTC),
        "scan_finished": datetime(2026, 8, 17, 19, 56, 35, tzinfo=UTC),
        "coverage": COVERAGE,
        "token_usage": DastTokenUsage(
            total=TOTAL,
            by_phase=BY_PHASE,
            by_agent_type=BY_AGENT,
            economy=ECONOMY,
            accounting_consistent=True,
        ),
    }
    values.update(overrides)
    return ValidatedDastRunMetadata(**values)


def _report(metadata: ValidatedDastRunMetadata | None = None) -> ValidatedDastReport:
    metadata = metadata or _metadata()
    return ValidatedDastReport(
        run_id=metadata.run_id,
        target_id=metadata.target_id,
        source_commits=(),
        findings_count=0,
        canonical_json=b"{}",
        run_metadata=metadata,
    )


class _Carrier:

    """Stands in for a pipeline that carries a row, without needing the database."""

    def __init__(self, metadata: ValidatedDastRunMetadata) -> None:
        self.dast_run_metadata = DastRunMetadata.objects.build_from_report(metadata)


class _Bare:

    """Stands in for a pipeline with no accepted DAST report."""

    @property
    def dast_run_metadata(self):
        raise DastRunMetadata.DoesNotExist


class DastRunMetadataDerivationTests(AISTApiBase):

    """Everything the panel shows that is not a raw column."""

    def test_the_display_numbers_come_out_of_the_reported_counters(self):
        detail = dast_run_detail(_Carrier(_metadata()))

        # thinking is already counted inside output, so the headline must not add it again.
        self.assertEqual(detail["total_tokens"], GRAND_TOTAL)
        self.assertNotEqual(detail["total_tokens"], GRAND_TOTAL + TOTAL.thinking_tokens)
        self.assertEqual(detail["duration_seconds"], 8329)
        self.assertEqual(detail["beyond_plan"], 1)
        self.assertEqual(detail["agents"], 22)
        self.assertEqual(detail["tokens"]["thinking_tokens"], 331554)

    def test_each_bucket_carries_the_total_its_segment_is_sized_by(self):
        detail = dast_run_detail(_Carrier(_metadata()))

        phases = {bucket["key"]: bucket["total_tokens"] for bucket in detail["token_by_phase"]}
        self.assertEqual(phases["6"], 1320 + 584186 + 1647095 + 54591759)
        self.assertEqual(phases["4"], 28 + 6819 + 40321 + 2186768)

    def test_a_total_is_withheld_when_one_of_its_components_went_unreported(self):
        partial = _metadata(
            token_usage=DastTokenUsage(total=DastTokenBucket(input_tokens=10, output_tokens=20), by_phase=None),
        )

        detail = dast_run_detail(_Carrier(partial))

        # A partial sum would read as authoritative while being wrong.
        self.assertIsNone(detail["total_tokens"])
        self.assertEqual(detail["tokens"]["output_tokens"], 20)

    def test_beyond_plan_falls_back_to_the_counts_when_the_names_are_absent(self):
        counts_only = _metadata(
            coverage=DastCoverage(unit="endpoint", analysed=38, planned=10),
        )

        self.assertEqual(dast_run_detail(_Carrier(counts_only))["beyond_plan"], 28)

    def test_a_run_inside_its_plan_reports_no_overshoot(self):
        inside = _metadata(coverage=DastCoverage(analysed=8, planned=10))

        self.assertEqual(dast_run_detail(_Carrier(inside))["beyond_plan"], 0)

    def test_an_impossible_duration_is_not_shown(self):
        reversed_clock = _metadata(
            scan_started=datetime(2026, 8, 17, 19, 56, 35, tzinfo=UTC),
            scan_finished=datetime(2026, 8, 17, 17, 37, 46, tzinfo=UTC),
        )

        self.assertIsNone(dast_run_detail(_Carrier(reversed_clock))["duration_seconds"])

    def test_agent_count_is_withheld_when_a_bucket_did_not_report_one(self):
        missing_agents = _metadata(
            token_usage=DastTokenUsage(total=TOTAL, by_agent_type=(DastTokenBucket(key="orchestrator"),)),
        )

        self.assertIsNone(dast_run_detail(_Carrier(missing_agents))["agents"])

    def test_a_pipeline_with_no_accepted_report_reads_as_absent_rather_than_raising(self):
        self.assertIsNone(dast_run_summary(_Bare()))
        self.assertIsNone(dast_run_detail(_Bare()))

    def test_the_import_preview_shows_exactly_what_the_list_will_show(self):
        metadata = _metadata()

        self.assertEqual(reported_dast_run_preview(metadata), dast_run_summary(_Carrier(metadata)))
        self.assertIsNone(reported_dast_run_preview(None))


class DastRunMetadataReadPathTests(AISTApiBase):

    """The pipeline list and the detail endpoint, across execution types and tenants."""

    def setUp(self):
        super().setUp()
        # The pipeline list is a session-authenticated portal view, unlike the DRF endpoints.
        self.client.force_login(self.user)
        self.sast = AISTPipeline.objects.create(
            id="run-meta-sast",
            project=self.project,
            project_version=self.pv,
            execution_type=PipelineExecutionType.SAST,
            status=AISTStatus.FINISHED,
        )
        self.imported = AISTPipeline.objects.create(
            id="run-meta-import",
            project=self.project,
            execution_type=PipelineExecutionType.MANUAL_IMPORT,
            status=AISTStatus.FINISHED,
        )
        DastRunMetadata.objects.upsert_from_report(pipeline_id=self.imported.id, report=_report())
        self.foreign = AISTPipeline.objects.create(
            id="run-meta-foreign",
            project=self.other_project,
            execution_type=PipelineExecutionType.MANUAL_IMPORT,
            status=AISTStatus.FINISHED,
        )
        DastRunMetadata.objects.upsert_from_report(pipeline_id=self.foreign.id, report=_report())

    def _summary_rows(self):
        response = self.client.get(reverse("client_pipeline_summary"))
        self.assertEqual(response.status_code, 200)
        return {row["id"]: row for row in response.json().get("results", [])}

    def test_the_list_serves_both_execution_types_and_only_the_dast_one_carries_a_run(self):
        rows = self._summary_rows()

        self.assertIsNone(rows[self.sast.id]["dast_run"])
        run = rows[self.imported.id]["dast_run"]
        self.assertEqual(run["analysed"], 38)
        self.assertEqual(run["reachable"], 176)
        self.assertEqual(run["beyond_plan"], 1)
        self.assertEqual(run["total_tokens"], GRAND_TOTAL)
        # The list stays counters-only: the inventory belongs to the detail endpoint.
        self.assertNotIn("analysed_names", run)

    def test_the_list_does_not_pay_an_extra_query_per_pipeline_carrying_a_run(self):
        """The reverse one-to-one is joined, so adding DAST rows must not add queries."""
        with CaptureQueriesContext(connection) as before:
            self._summary_rows()

        for index in range(3):
            extra = AISTPipeline.objects.create(
                id=f"run-meta-extra-{index}",
                project=self.project,
                execution_type=PipelineExecutionType.MANUAL_IMPORT,
                status=AISTStatus.FINISHED,
            )
            DastRunMetadata.objects.upsert_from_report(pipeline_id=extra.id, report=_report())

        with CaptureQueriesContext(connection) as after:
            rows = self._summary_rows()

        self.assertEqual(len(after), len(before))
        self.assertEqual(sum(1 for row in rows.values() if row["dast_run"]), 4)

    def test_the_detail_endpoint_serves_the_inventory_to_the_owning_organization(self):
        response = self.client.get(reverse("aist_api:pipeline_dast_run", kwargs={"pipeline_id": self.imported.id}))

        self.assertEqual(response.status_code, 200)
        payload = response.json()["dast_run"]
        self.assertEqual(payload["stand_id"], "external-10host")
        self.assertEqual(payload["target_host"], "analytics3.test.hdw.mx")
        self.assertEqual(len(payload["analysed_names"]), 3)
        self.assertEqual(payload["beyond_plan_names"], ["cloud-prod-hdw-mx"])
        self.assertEqual(payload["agents"], 22)
        self.assertEqual({bucket["key"] for bucket in payload["token_by_phase"]}, {"6", "4"})
        self.assertEqual(payload["economy"], {
            **{key: value for key, value in ECONOMY.as_wire().items() if key != "tokens"},
            "total_tokens": ECONOMY.tokens,
        })

    def test_the_detail_endpoint_returns_null_for_a_pipeline_without_an_accepted_report(self):
        response = self.client.get(reverse("aist_api:pipeline_dast_run", kwargs={"pipeline_id": self.sast.id}))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["dast_run"])

    def test_the_detail_endpoint_refuses_a_pipeline_from_another_organization(self):
        response = self.client.get(reverse("aist_api:pipeline_dast_run", kwargs={"pipeline_id": self.foreign.id}))

        self.assertEqual(response.status_code, 404)
