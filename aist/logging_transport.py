# aist/logging_transport.py

import logging
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

import redis
from django.conf import settings

REDIS_URL = getattr(settings, "CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
PUBSUB_CHANNEL_TPL = "aist:pipeline:{pipeline_id}:logs"
STREAM_KEY = "aist:logs"
BACKLOG_COUNT = 200

_logger = logging.getLogger(__name__)


def get_redis():
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def get_pipeline_log_path(pipeline_id: str) -> Path:
    """
    Returns the absolute filesystem path to the pipeline log file.
    Used both by logging setup and by log reading APIs in views.
    """
    media_root = getattr(settings, "MEDIA_ROOT", "media")
    log_dir = Path(media_root) / "aist_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{pipeline_id}.log"


def install_pipeline_logging(pipeline_id: str, level=logging.INFO) -> logging.Logger:
    def _attach_handler(logger: logging.Logger, handler: logging.Handler) -> None:
        """
        Attach handler to logger only if a RotatingFileHandler
        pointing to the same log file is not already attached.
        """
        if not any(
                isinstance(h, RotatingFileHandler)
                and getattr(h, "pipeline_id", None) == pipeline_id
                for h in logger.handlers
        ):
            logger.addHandler(handler)
        logger.setLevel(level)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # Build log file path
    log_path = get_pipeline_log_path(pipeline_id)

    # Create rotating file handler
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.pipeline_id = pipeline_id
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)

    plog = logging.getLogger("pipeline")
    plog.propagate = True
    _attach_handler(plog, file_handler)

    root = logging.getLogger(f"aist.pipeline.{pipeline_id}")
    _attach_handler(root, file_handler)

    return root


def uninstall_pipeline_file_logging(pipeline_id: str):
    plog = logging.getLogger("pipeline")
    root = logging.getLogger(f"aist.pipeline.{pipeline_id}")

    for logger in (plog, root):
        for h in list(logger.handlers):
            if getattr(h, "pipeline_id", None) == pipeline_id:
                logger.removeHandler(h)
                with suppress(Exception):
                    h.close()
