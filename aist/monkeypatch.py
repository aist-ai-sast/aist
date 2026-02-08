from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from django.db import transaction
from dojo.models import Finding

from aist.signals import finding_deduplicated

try:
    from dojo.finding import deduplication as dedupe_mod
except Exception:  # pragma: no cover - defensive import guard
    dedupe_mod = None

logger = logging.getLogger(__name__)
_patch_lock = threading.Lock()
_patched = threading.Event()

if TYPE_CHECKING:
    from collections.abc import Callable


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


def _rebind_imported_symbol(module_path: str, name: str, wrapped: Callable[..., Any]) -> None:
    try:
        module = __import__(module_path, fromlist=[name])
    except Exception:
        return

    current = getattr(module, name, None)
    if current is None:
        return
    if getattr(current, "__wrapped__", None):
        return

    setattr(module, name, wrapped)


def install_deduplication_monkeypatch() -> None:
    with _patch_lock:
        if _patched.is_set():
            return

        if dedupe_mod is None:
            logger.error("Failed to import dojo.finding.deduplication for monkeypatch")
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

        batch_task = getattr(dedupe_mod, "do_dedupe_batch_task", None)
        if batch_task is None:
            logger.warning("do_dedupe_batch_task not found in dojo.finding.deduplication; skipping batch monkeypatch")
        elif getattr(batch_task, "__wrapped__", None):
            logger.warning("do_dedupe_batch_task already wrapped; skipping monkeypatch")
        else:
            wrapped_batch_task = _wrap_dedupe_batch(batch_task)
            dedupe_mod.do_dedupe_batch_task = wrapped_batch_task
            _rebind_imported_symbol(
                "dojo.finding.helper",
                "do_dedupe_batch_task",
                wrapped_batch_task,
            )

        batch_findings = getattr(dedupe_mod, "dedupe_batch_of_findings", None)
        if batch_findings is None:
            logger.warning(
                "dedupe_batch_of_findings not found in dojo.finding.deduplication; skipping batch-findings monkeypatch",
            )
        elif getattr(batch_findings, "__wrapped__", None):
            logger.warning("dedupe_batch_of_findings already wrapped; skipping monkeypatch")
        else:
            wrapped_batch_findings = _wrap_dedupe_batch(batch_findings)
            dedupe_mod.dedupe_batch_of_findings = wrapped_batch_findings
            _rebind_imported_symbol(
                "dojo.finding.helper",
                "dedupe_batch_of_findings",
                wrapped_batch_findings,
            )

        if single is not None and getattr(single, "__wrapped__", None) is None:
            _rebind_imported_symbol(
                "dojo.finding.helper",
                "do_dedupe_finding_task_internal",
                dedupe_mod.do_dedupe_finding_task_internal,
            )

        _patched.set()
