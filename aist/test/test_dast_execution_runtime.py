import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from celery.exceptions import Retry

from aist.integrations.resolver import ResolvedIntegration
from aist.models import (
    AISTPipeline,
    AISTStatus,
    DastExecutionState,
    PipelineExecutionType,
)
from aist.pipeline_args import DastPipelineArguments, PipelineArguments
from aist.services.dast_outcomes import DastRunState
from aist.tasks import dast_execution
from aist.test import dast_fixtures
from aist.test.test_api import AISTApiBase

CAPABILITY_REVISION = f"sha256:{'a' * 64}"


def _command():
    return dast_execution.DastStartCommand.from_wire(
        {
            "contract_version": "2.0",
            "idempotency_key": "launch-123",
            "correlation_id": "pipeline-123",
            "target_id": "cloud-backend",
            "capability_revision": CAPABILITY_REVISION,
            "trigger": {"repository_key": "backend", "type": "GIT_HASH", "ref": "b" * 40},
            "parameters": {"depth": "light"},
        },
    )


class DastExecutionRuntimeTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self._build_dir = tempfile.TemporaryDirectory()
        self._output_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._build_dir.cleanup)
        self.addCleanup(self._output_dir.cleanup)
        settings_override = self.settings(
            AIST_PROJECTS_BUILD_DIR=self._build_dir.name,
            AIST_OUTPUT_PATH=self._output_dir.name,
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

    def _runtime(self, *, vpn=None):
        command = _command()
        binding = SimpleNamespace(pk=123)
        arguments = PipelineArguments(
            project=self.project,
            payload=DastPipelineArguments(
                project=self.project,
                binding=binding,
                trigger_project_version=self.pv,
                parameters={"depth": "light"},
                capability={"repository_keys": ["backend"]},
            ),
        )
        return dast_execution._DastRuntimeSpec(
            gateway_url="https://dast.internal",
            command=command,
            token="runtime-token",  # noqa: S106 -- test fixture
            ca_bundle="runtime-ca",
            vpn=vpn,
            recovery=dast_execution.DastRecoveryState.initial(command),
            arguments=arguments,
            allowed_repository_keys=frozenset({"backend"}),
            stop_requested=False,
            binding=binding,
            lead=None,
        )

    @contextmanager
    def _executing(self, runtime, *, execute):
        """Run the executor around ``runtime`` with a recorded VPN sidecar and connector ``execute``."""
        observed = {}

        @contextmanager
        def fake_vpn_context(resolved, *, execution_id):
            observed["resolved"] = resolved
            observed["execution_id"] = execution_id
            try:
                yield ("vpn-pipeline-123" if resolved is not None else None), None
            finally:
                observed["sidecar_cleaned"] = True

        with (
            patch("aist.tasks.dast_execution._prepare_dast_runtime", return_value=runtime),
            patch("aist.tasks.dast_execution.vpn_sidecar_context", fake_vpn_context),
            patch("aist.tasks.dast_execution.execute_pipeline", side_effect=execute),
            patch("aist.tasks.dast_execution._persist_dast_execution_result") as persist_result,
        ):
            yield observed, persist_result

    def test_explicit_dast_vpn_is_the_only_network_and_secrets_are_ephemeral_files(self):
        vpn = ResolvedIntegration(integration=SimpleNamespace(pk=7), config={"profile": "explicit-dast-vpn"})
        observed = {}

        def fake_execute(execution_type, execution):
            observed["execution_type"] = execution_type
            observed["execution"] = execution
            observed["output_path"] = execution.output_dir
            execution.output_dir.mkdir(parents=True)
            (execution.output_dir / "durable-result.json").write_text("{}", encoding="utf-8")
            observed["token_path"] = execution.token_file
            observed["ca_path"] = execution.ca_file
            self.assertEqual(stat.S_IMODE(execution.token_file.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(execution.token_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(execution.ca_file.stat().st_mode), 0o600)
            self.assertEqual(execution.token_file.read_text(encoding="utf-8"), "runtime-token")
            self.assertEqual(execution.ca_file.read_text(encoding="utf-8"), "runtime-ca")
            return dast_fixtures.connector_result(state=dast_execution.DastConnectorOutcomeState.STOP_PENDING)

        with self._executing(self._runtime(vpn=vpn), execute=fake_execute) as (sidecar, _persist):
            dast_execution._execute_dast_pipeline("pipeline-123")

        self.assertIs(sidecar["resolved"], vpn)
        self.assertEqual(sidecar["execution_id"], "pipeline-123")
        self.assertEqual(observed["execution_type"], PipelineExecutionType.DAST)
        self.assertEqual(observed["execution"].vpn_container_name, "vpn-pipeline-123")
        self.assertTrue(sidecar["sidecar_cleaned"])
        self.assertFalse(observed["token_path"].exists())
        self.assertFalse(observed["ca_path"].exists())
        self.assertEqual(
            observed["output_path"],
            Path(self._output_dir.name) / self.product.name / self.pv.version / "pipeline-123",
        )
        self.assertTrue((observed["output_path"] / "durable-result.json").is_file())
        self.assertNotIn("runtime-token", repr(observed["execution"]))
        self.assertNotIn("runtime-ca", repr(observed["execution"]))

    def test_no_vpn_uses_direct_connector_policy_without_project_or_scm_fallback(self):
        observed = {}

        def fake_execute(_execution_type, execution):
            observed["execution"] = execution
            return dast_fixtures.connector_result(state=dast_execution.DastConnectorOutcomeState.STOP_PENDING)

        with self._executing(self._runtime(), execute=fake_execute) as (sidecar, _persist):
            dast_execution._execute_dast_pipeline("pipeline-123")

        self.assertIsNone(sidecar["resolved"])
        self.assertEqual(sidecar["execution_id"], "pipeline-123")
        self.assertIsNone(observed["execution"].vpn_container_name)
        self.assertFalse(hasattr(observed["execution"], "project"))
        self.assertFalse(hasattr(observed["execution"], "scm_integration"))

    def test_executor_failure_still_cleans_sidecar_and_secret_workspace(self):
        vpn = ResolvedIntegration(integration=SimpleNamespace(pk=7), config={"profile": "explicit-dast-vpn"})
        observed = {}

        def fail_execute(_execution_type, execution):
            observed["token_path"] = execution.token_file
            observed["ca_path"] = execution.ca_file
            error_message = "connector failed"
            raise RuntimeError(error_message)

        with (
            self._executing(self._runtime(vpn=vpn), execute=fail_execute) as (sidecar, _persist),
            self.assertRaisesRegex(RuntimeError, "connector failed"),
        ):
            dast_execution._execute_dast_pipeline("pipeline-123")

        self.assertEqual(sidecar["execution_id"], "pipeline-123")
        self.assertTrue(sidecar["sidecar_cleaned"])
        self.assertFalse(observed["token_path"].exists())
        self.assertFalse(observed["ca_path"].exists())

    @staticmethod
    def _terminal_result(report_path, *, status=DastRunState.SUCCEEDED):
        return dast_fixtures.connector_result(status=status, report_path=report_path)

    def test_invalid_terminal_report_is_rejected_after_execution_identity_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "dast_result.json"
            report_path.write_text("{}", encoding="utf-8")
            runtime = self._runtime()
            execution_result = self._terminal_result(report_path)

            with (
                self._executing(runtime, execute=lambda *_args: execution_result) as (sidecar, persist_result),
                patch(
                    "aist.tasks.dast_execution.validate_dast_report_bytes",
                    side_effect=ValueError("invalid provider report"),
                ) as validate_report,
                self.assertRaisesRegex(ValueError, "invalid provider report"),
            ):
                dast_execution._execute_dast_pipeline("pipeline-123")

            self.assertEqual(sidecar["execution_id"], "pipeline-123")
            validate_report.assert_called_once_with(
                b"{}",
                target_id="cloud-backend",
                allowed_repository_keys=frozenset({"backend"}),
                expected_run_id="run-123",
            )
            persist_result.assert_called_once_with("pipeline-123", execution_result)

    def test_execution_checkpoint_precedes_file_validation_and_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "dast_result.json"
            report_path.write_text("{}", encoding="utf-8")
            runtime = self._runtime()
            execution_result = self._terminal_result(report_path)
            validated_report = SimpleNamespace(run_id="run-123")
            call_order = []

            with (
                self._executing(runtime, execute=lambda *_args: execution_result) as (_sidecar, persist_result),
                patch(
                    "aist.tasks.dast_execution.validate_dast_report_bytes",
                    side_effect=lambda *_args, **_kwargs: call_order.append("validate") or validated_report,
                ),
                patch(
                    "aist.tasks.dast_execution.finalize_dast_report",
                    side_effect=lambda **_kwargs: call_order.append("finalize"),
                ) as finalize_report,
            ):
                persist_result.side_effect = lambda *_args: call_order.append("persist")
                dast_execution._execute_dast_pipeline("pipeline-123")

            self.assertEqual(call_order, ["persist", "validate", "finalize"])
            finalize_report.assert_called_once()
            self.assertIs(finalize_report.call_args.kwargs["report"], validated_report)
            self.assertIs(finalize_report.call_args.kwargs["binding"], runtime.binding)

    def test_a_result_carrying_a_report_is_finalized_and_one_without_is_not(self):
        cases = {
            DastRunState.SUCCEEDED: True,
            DastRunState.COMPLETED_WITH_DEGRADATION: True,
            DastRunState.FAILED_WITH_PARTIAL_RESULTS: True,
            DastRunState.FAILED: False,
            DastRunState.STOPPED: False,
        }
        for status, finalized in cases.items():
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / "dast_result.json"
                report_path.write_text("{}", encoding="utf-8")
                execution_result = self._terminal_result(report_path, status=status)

                with (
                    self._executing(self._runtime(), execute=lambda *_args, result=execution_result: result),
                    patch("aist.tasks.dast_execution.validate_dast_report_bytes"),
                    patch("aist.tasks.dast_execution.finalize_dast_report") as finalize_report,
                ):
                    dast_execution._execute_dast_pipeline("pipeline-123")

                self.assertEqual(finalize_report.call_count, 1 if finalized else 0)


class DastUnsettledResultTests(AISTApiBase):

    """A connector result the provider has not settled yet is retried, never recorded or finished."""

    def test_stop_pending_and_unreachable_results_are_retried_without_an_outcome(self):
        for state in (
            dast_execution.DastConnectorOutcomeState.STOP_PENDING,
            dast_execution.DastConnectorOutcomeState.UNREACHABLE,
        ):
            with self.subTest(state=state):
                task = dast_fixtures.FakeRetryingTask(retries=2)
                result = dast_fixtures.connector_result(state=state)

                with (
                    patch("aist.tasks.dast_execution.record_dast_pipeline_outcome") as record_outcome,
                    patch("aist.tasks.dast_execution.finish_pipeline") as finish_pipeline,
                    self.assertRaises(Retry),
                ):
                    dast_execution._handle_dast_execution_result(task, "pipeline-123", result)

                record_outcome.assert_not_called()
                finish_pipeline.assert_not_called()
                self.assertEqual(task.retry_calls, [{"exc": None, "countdown": 60}])


class DastRunProgressTests(AISTApiBase):

    """
    What the platform knows about a run while it is still going.

    The connector reports its recovery state as soon as the provider accepts the run. Until that
    reaches durable state the platform has no run id, and everything addressed by one -- the stall
    bound, the run's work queue -- has nothing to address for the whole length of the run.
    """

    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="dastprogress1",
            project=self.project,
            project_version=self.pv,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.EXECUTING,
        )
        self.state = DastExecutionState.objects.create(pipeline=self.pipeline)

    def _recovery(self, *, run_id, log_cursor=0):
        return SimpleNamespace(run_id=run_id, log_cursor=log_cursor, correlation_id=self.pipeline.id)

    def test_the_run_identity_is_durable_as_soon_as_the_connector_reports_it(self):
        dast_execution._record_dast_progress(self.pipeline.id, self._recovery(run_id="run-123"))

        self.state.refresh_from_db()
        self.assertEqual(self.state.run_id, "run-123")
        self.assertIsNotNone(self.state.last_progress_at)

    def test_further_log_events_count_as_a_sign_of_life(self):
        dast_execution._record_dast_progress(self.pipeline.id, self._recovery(run_id="run-123"))
        self.state.refresh_from_db()
        first_seen = self.state.last_progress_at

        dast_execution._record_dast_progress(
            self.pipeline.id, self._recovery(run_id="run-123", log_cursor=12),
        )

        self.state.refresh_from_db()
        self.assertEqual(self.state.log_cursor, 12)
        self.assertGreater(self.state.last_progress_at, first_seen)

    def test_a_report_that_advances_nothing_leaves_the_sign_of_life_alone(self):
        dast_execution._record_dast_progress(
            self.pipeline.id, self._recovery(run_id="run-123", log_cursor=12),
        )
        self.state.refresh_from_db()
        first_seen = self.state.last_progress_at

        dast_execution._record_dast_progress(
            self.pipeline.id, self._recovery(run_id="run-123", log_cursor=12),
        )

        self.state.refresh_from_db()
        self.assertEqual(self.state.last_progress_at, first_seen)

    def test_a_reported_run_is_addressable_before_it_finishes(self):
        # This is what the queue block on the pipeline page waits for.
        self.assertFalse(bool(self.state.run_id))
        dast_execution._record_dast_progress(self.pipeline.id, self._recovery(run_id="run-123"))

        self.pipeline.refresh_from_db()
        self.assertEqual(self.pipeline.dast_execution_state.run_id, "run-123")
