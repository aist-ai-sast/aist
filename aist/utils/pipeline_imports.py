from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

from django.conf import settings

_logger = logging.getLogger(__name__)


def _import_sast_pipeline_package() -> None:
    pipeline_path = getattr(settings, "AIST_PIPELINE_CODE_PATH", None)
    if not pipeline_path or not Path(pipeline_path).is_dir():
        msg = (
            "SAST pipeline code path is not configured or does not exist. "
            "Please set AIST_PIPELINE_CODE_PATH."
        )
        raise RuntimeError(msg)
    # Warn if the directory is world-writable: a write there means arbitrary
    # Python code injection into the Celery worker process (sys.path poisoning).
    try:
        mode = Path(pipeline_path).stat().st_mode
        if mode & 0o002:
            _logger.warning(
                "AIST_PIPELINE_CODE_PATH is world-writable (%#o). "
                "This allows arbitrary code injection into the Celery worker. "
                "Fix permissions: chmod o-w '%s'",
                mode,
                pipeline_path,
            )
    except OSError:
        pass
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
