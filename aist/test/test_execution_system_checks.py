from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from django.core.checks import run_checks
from django.test import SimpleTestCase, override_settings

from aist.checks import AIST_EXECUTION_CHECK_TAG

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_ROOT = PROJECT_ROOT / "sast-combinator" / "sast-pipeline"


class ExecutionSystemCheckTests(SimpleTestCase):
    @override_settings(AIST_PIPELINE_CODE_PATH=str(PIPELINE_ROOT))
    def test_production_runtime_has_catalog_dispatcher_executor_image_and_lease(self):
        errors = run_checks(tags=[AIST_EXECUTION_CHECK_TAG], include_deployment_checks=True)

        self.assertEqual(errors, [])

    def test_missing_runtime_fails_closed(self):
        with TemporaryDirectory() as temporary_directory, override_settings(
            AIST_PIPELINE_CODE_PATH=temporary_directory,
        ):
            errors = run_checks(tags=[AIST_EXECUTION_CHECK_TAG], include_deployment_checks=True)

        self.assertEqual([error.id for error in errors], ["aist.E002"])

    def test_mixed_analyzer_dast_declaration_fails_closed(self):
        with TemporaryDirectory() as temporary_directory:
            pipeline_root = Path(temporary_directory)
            (pipeline_root / "pipeline" / "dast").mkdir(parents=True)
            (pipeline_root / "pipeline" / "config").mkdir(parents=True)
            (pipeline_root / "Dockerfiles" / "dast_connector").mkdir(parents=True)
            for relative_path in (
                "pipeline/execution.py",
                "pipeline/dast/executor.py",
                "Dockerfiles/dast_connector/Dockerfile",
            ):
                (pipeline_root / relative_path).touch()
            catalog = {
                "analyzers": [
                    {
                        "name": "dast",
                        "type": "simple",
                        "execution_type": "dast",
                        "image": "aist-dast-connector:v2",
                    },
                ],
            }
            (pipeline_root / "pipeline" / "config" / "analyzers.yaml").write_text(
                yaml.safe_dump(catalog),
                encoding="utf-8",
            )
            with override_settings(AIST_PIPELINE_CODE_PATH=temporary_directory):
                errors = run_checks(tags=[AIST_EXECUTION_CHECK_TAG], include_deployment_checks=True)

        self.assertEqual([error.id for error in errors], ["aist.E005"])
