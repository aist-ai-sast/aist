import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aist.models import PipelineExecutionType
from aist.pipeline_args import DastPipelineArguments, PipelineArguments
from aist.tasks import pipeline as pipeline_tasks
from aist.test.test_api import AISTApiBase

CAPABILITY_REVISION = f"sha256:{'a' * 64}"


def _command():
    return pipeline_tasks.DastStartCommand.from_wire(
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

    def _runtime(self, *, vpn_integration=None):
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
        return pipeline_tasks._DastRuntimeSpec(
            gateway_url="https://dast.internal",
            command=command,
            token="runtime-token",  # noqa: S106 -- test fixture
            ca_bundle="runtime-ca",
            vpn_integration=vpn_integration,
            recovery=pipeline_tasks.DastRecoveryState.initial(command),
            arguments=arguments,
            allowed_repository_keys=frozenset({"backend"}),
            stop_requested=False,
            binding=binding,
            lead=None,
        )

    def test_explicit_dast_vpn_is_the_only_network_and_secrets_are_ephemeral_files(self):
        vpn = SimpleNamespace(config={"profile": "explicit-dast-vpn"})
        observed = {}

        @contextmanager
        def fake_vpn_context(resolved, *, execution_id):
            observed["resolved"] = resolved
            observed["execution_id"] = execution_id
            try:
                yield "vpn-pipeline-123", None
            finally:
                observed["sidecar_cleaned"] = True

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
            return SimpleNamespace(
                recovery=execution.recovery,
                outcome=SimpleNamespace(state=pipeline_tasks.DastConnectorOutcomeState.STOP_PENDING),
            )

        with (
            patch("aist.tasks.pipeline._prepare_dast_runtime", return_value=self._runtime(vpn_integration=vpn)),
            patch("aist.tasks.pipeline.vpn_sidecar_context", fake_vpn_context),
            patch("aist.tasks.pipeline.execute_pipeline", side_effect=fake_execute),
            patch("aist.tasks.pipeline._persist_dast_execution_result"),
        ):
            pipeline_tasks._execute_dast_pipeline("pipeline-123")

        self.assertIs(observed["resolved"].integration, vpn)
        self.assertEqual(observed["resolved"].config, vpn.config)
        self.assertEqual(observed["execution_id"], "pipeline-123")
        self.assertEqual(observed["execution_type"], PipelineExecutionType.DAST)
        self.assertEqual(observed["execution"].vpn_container_name, "vpn-pipeline-123")
        self.assertTrue(observed["sidecar_cleaned"])
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

        @contextmanager
        def fake_vpn_context(resolved, *, execution_id):
            observed["resolved"] = resolved
            observed["execution_id"] = execution_id
            yield None, None

        def fake_execute(_execution_type, execution):
            observed["execution"] = execution
            return SimpleNamespace(
                recovery=execution.recovery,
                outcome=SimpleNamespace(state=pipeline_tasks.DastConnectorOutcomeState.STOP_PENDING),
            )

        with (
            patch("aist.tasks.pipeline._prepare_dast_runtime", return_value=self._runtime()),
            patch("aist.tasks.pipeline.vpn_sidecar_context", fake_vpn_context),
            patch("aist.tasks.pipeline.execute_pipeline", side_effect=fake_execute),
            patch("aist.tasks.pipeline._persist_dast_execution_result"),
        ):
            pipeline_tasks._execute_dast_pipeline("pipeline-123")

        self.assertIsNone(observed["resolved"])
        self.assertEqual(observed["execution_id"], "pipeline-123")
        self.assertIsNone(observed["execution"].vpn_container_name)
        self.assertFalse(hasattr(observed["execution"], "project"))
        self.assertFalse(hasattr(observed["execution"], "scm_integration"))

    def test_executor_failure_still_cleans_sidecar_and_secret_workspace(self):
        vpn = SimpleNamespace(config={"profile": "explicit-dast-vpn"})
        observed = {}

        @contextmanager
        def fake_vpn_context(_resolved, *, execution_id):
            observed["execution_id"] = execution_id
            try:
                yield "vpn-pipeline-123", None
            finally:
                observed["sidecar_cleaned"] = True

        def fail_execute(_execution_type, execution):
            observed["token_path"] = execution.token_file
            observed["ca_path"] = execution.ca_file
            error_message = "connector failed"
            raise RuntimeError(error_message)

        with (
            patch("aist.tasks.pipeline._prepare_dast_runtime", return_value=self._runtime(vpn_integration=vpn)),
            patch("aist.tasks.pipeline.vpn_sidecar_context", fake_vpn_context),
            patch("aist.tasks.pipeline.execute_pipeline", side_effect=fail_execute),
            patch("aist.tasks.pipeline._persist_dast_execution_result"),
            self.assertRaisesRegex(RuntimeError, "connector failed"),
        ):
            pipeline_tasks._execute_dast_pipeline("pipeline-123")

        self.assertTrue(observed["sidecar_cleaned"])
        self.assertFalse(observed["token_path"].exists())
        self.assertFalse(observed["ca_path"].exists())

    def test_invalid_terminal_report_is_rejected_after_execution_identity_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "dast_result.json"
            report_path.write_text("{}", encoding="utf-8")
            runtime = self._runtime()
            recovery = runtime.recovery.for_run("run-123")
            execution_result = SimpleNamespace(
                recovery=recovery,
                terminal_result=SimpleNamespace(status=SimpleNamespace(value="succeeded")),
                outcome=SimpleNamespace(state=pipeline_tasks.DastConnectorOutcomeState.TERMINAL),
                report_path=report_path,
            )

            @contextmanager
            def fake_vpn_context(_resolved, *, execution_id):
                self.assertEqual(execution_id, "pipeline-123")
                yield None, None

            with (
                patch("aist.tasks.pipeline._prepare_dast_runtime", return_value=runtime),
                patch("aist.tasks.pipeline.vpn_sidecar_context", fake_vpn_context),
                patch("aist.tasks.pipeline.execute_pipeline", return_value=execution_result),
                patch(
                    "aist.tasks.pipeline.validate_dast_report_bytes",
                    side_effect=ValueError("invalid provider report"),
                ) as validate_report,
                patch("aist.tasks.pipeline._persist_dast_execution_result") as persist_result,
                self.assertRaisesRegex(ValueError, "invalid provider report"),
            ):
                pipeline_tasks._execute_dast_pipeline("pipeline-123")

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
            recovery = runtime.recovery.for_run("run-123")
            execution_result = SimpleNamespace(
                recovery=recovery,
                terminal_result=SimpleNamespace(status=SimpleNamespace(value="succeeded")),
                outcome=SimpleNamespace(state=pipeline_tasks.DastConnectorOutcomeState.TERMINAL),
                report_path=report_path,
            )
            validated_report = SimpleNamespace(run_id="run-123")
            call_order = []

            @contextmanager
            def fake_vpn_context(_resolved, *, execution_id):
                self.assertEqual(execution_id, "pipeline-123")
                yield None, None

            with (
                patch("aist.tasks.pipeline._prepare_dast_runtime", return_value=runtime),
                patch("aist.tasks.pipeline.vpn_sidecar_context", fake_vpn_context),
                patch("aist.tasks.pipeline.execute_pipeline", return_value=execution_result),
                patch(
                    "aist.tasks.pipeline.validate_dast_report_bytes",
                    side_effect=lambda *_args, **_kwargs: call_order.append("validate") or validated_report,
                ),
                patch(
                    "aist.tasks.pipeline.finalize_dast_report",
                    side_effect=lambda **_kwargs: call_order.append("finalize"),
                ) as finalize_report,
                patch(
                    "aist.tasks.pipeline._persist_dast_execution_result",
                    side_effect=lambda *_args: call_order.append("persist"),
                ),
            ):
                pipeline_tasks._execute_dast_pipeline("pipeline-123")

            self.assertEqual(call_order, ["persist", "validate", "finalize"])
            finalize_report.assert_called_once()
            self.assertIs(finalize_report.call_args.kwargs["report"], validated_report)
            self.assertIs(finalize_report.call_args.kwargs["binding"], runtime.binding)
