"""Serve launches and launch forms a synthetic sast-pipeline analyzer catalog."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.config_utils import AnalyzersConfigHelper  # noqa: E402


def write_analyzers_config(directory: Path, analyzers: list[dict]) -> str:
    """Write ``analyzers`` as the analyzers.yaml sast-pipeline reads in ``directory``, and return its path."""
    path = Path(directory) / "analyzers.yaml"
    path.write_text(yaml.safe_dump({"analyzers": analyzers}), encoding="utf-8")
    return str(path)


class SyntheticAnalyzerCatalogMixin:

    """
    Replaces sast-pipeline's analyzer catalog with ``CATALOG`` for the test.

    Both the launch's analyzer selection and the launch form's choices read it.
    """

    CATALOG: dict = {}

    def setUp(self):
        super().setUp()
        catalog_dir = tempfile.TemporaryDirectory()
        self.addCleanup(catalog_dir.cleanup)
        catalog = AnalyzersConfigHelper(
            write_analyzers_config(Path(catalog_dir.name), self.CATALOG.get("analyzers", [])),
        )
        for target in ("aist.pipeline_args._load_analyzers_config", "aist.forms._load_analyzers_config"):
            loader = patch(target, return_value=catalog)
            loader.start()
            self.addCleanup(loader.stop)
