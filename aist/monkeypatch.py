from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from django.db import transaction

from aist.signals import finding_deduplicated

logger = logging.getLogger(__name__)
_patch_lock = threading.Lock()
_patched = False


def _emit_for_finding(finding) -> None:  # type: ignore[no-untyped-def]
    try:
        test = getattr(finding, "test", None)
        if getattr(finding, "id", None) and test is not None:
            transaction.on_commit(
                lambda: finding_deduplicated.send(
                    sender=type(finding),
                    finding_id=finding.id,
                    test=test,
                ),
            )
    except Exception:
        logger.exception("Failed to emit finding_deduplicated")


def _wrap_dedupe_single(func: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(new_finding, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = func(new_finding, *args, **kwargs)
        _emit_for_finding(new_finding)
        return result

    wrapper.__name__ = getattr(func, "__name__", "do_dedupe_finding")
    wrapper.__doc__ = getattr(func, "__doc__", None)
    wrapper.__wrapped__ = func  # type: ignore[attr-defined]
    return wrapper


def _emit_for_finding_ids(finding_ids) -> None:  # type: ignore[no-untyped-def]
    try:
        from dojo.models import Finding  # noqa: PLC0415
    except Exception:
        logger.exception("Failed to import Finding for batch dedupe signal emission")
        return

    for finding in Finding.objects.filter(id__in=list(finding_ids)).select_related("test"):
        _emit_for_finding(finding)


def _wrap_dedupe_batch(func: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(finding_ids, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = func(finding_ids, *args, **kwargs)
        try:
            if not finding_ids:
                return result
            first = next(iter(finding_ids))
            if hasattr(first, "id") and hasattr(first, "test"):
                for finding in finding_ids:
                    _emit_for_finding(finding)
            else:
                _emit_for_finding_ids(finding_ids)
        except Exception:
            logger.exception("Failed to emit finding_deduplicated (batch)")
        return result

    wrapper.__name__ = getattr(func, "__name__", "dedupe_batch_of_findings")
    wrapper.__doc__ = getattr(func, "__doc__", None)
    wrapper.__wrapped__ = func  # type: ignore[attr-defined]
    return wrapper


def install_deduplication_monkeypatch() -> None:
    global _patched
    with _patch_lock:
        if _patched:
            return

        try:
            from dojo.finding import deduplication as dedupe_mod
        except Exception:
            logger.exception("Failed to import dojo.finding.deduplication for monkeypatch")
            return

        single = getattr(dedupe_mod, "do_dedupe_finding_task_internal", None)
        if single is None:
            logger.warning(
                "do_dedupe_finding_task_internal not found in dojo.finding.deduplication; skipping single monkeypatch",
            )
        elif getattr(single, "__wrapped__", None):
            logger.warning("do_dedupe_finding_task_internal already wrapped; skipping monkeypatch")
        else:
            dedupe_mod.do_dedupe_finding_task_internal = _wrap_dedupe_single(single)

        batch = getattr(dedupe_mod, "do_dedupe_batch_task", None)
        if batch is None:
            logger.warning("do_dedupe_batch_task not found in dojo.finding.deduplication; skipping batch monkeypatch")
        elif getattr(batch, "__wrapped__", None):
            logger.warning("do_dedupe_batch_task already wrapped; skipping monkeypatch")
        else:
            dedupe_mod.do_dedupe_batch_task = _wrap_dedupe_batch(batch)

        _patched = True
