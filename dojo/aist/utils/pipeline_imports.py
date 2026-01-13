from __future__ import annotations

import importlib
import sys
from pathlib import Path

from django.conf import settings


def _import_sast_pipeline_package() -> None:
    pipeline_path = getattr(settings, "AIST_PIPELINE_CODE_PATH", None)
    if not pipeline_path or not Path(pipeline_path).is_dir():
        msg = (
            "SAST pipeline code path is not configured or does not exist. "
            "Please set AIST_PIPELINE_CODE_PATH."
        )
        raise RuntimeError(msg)
    if pipeline_path not in sys.path:
        sys.path.append(pipeline_path)


# Must run before importing modules from the external "pipeline" package
_import_sast_pipeline_package()
from pipeline.docker_utils import (  # type: ignore[import-not-found]  # noqa: E402
    cleanup_pipeline_containers as _cleanup_pipeline_containers,
)


def cleanup_pipeline_containers(*args, **kwargs):
    return _cleanup_pipeline_containers(*args, **kwargs)


def _load_analyzers_config():
    _import_sast_pipeline_package()
    return importlib.import_module("pipeline.config_utils").AnalyzersConfigHelper()
