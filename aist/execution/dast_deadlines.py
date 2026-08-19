"""
The single answer to "is this DAST run over?", asked by retries, reconciliation and cancellation.

Two independent bounds: the execution timeout caps total run length (a safety ceiling), the stall
timeout caps how long the provider may show no sign of life. Both, and the grace window, are
required settings -- ``aist_site.settings`` owns their defaults, so none is repeated here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone


def _optional_window(seconds: int) -> timedelta | None:
    """Zero or less means the bound is off, not a zero-length one."""
    return timedelta(seconds=seconds) if seconds > 0 else None


def dast_execution_timeout() -> timedelta | None:
    """How long one DAST run may take before its deadline passes; None when uncapped."""
    return _optional_window(settings.AIST_DAST_EXECUTION_TIMEOUT_SECONDS)


def dast_unreachable_grace() -> timedelta:
    """How long an unreachable provider is retried past the deadline before giving up."""
    return timedelta(seconds=max(0, settings.AIST_DAST_UNREACHABLE_GRACE_SECONDS))


def dast_provider_stall_timeout() -> timedelta | None:
    """How long the provider may report no progress before the run is given up; None when uncapped."""
    return _optional_window(settings.AIST_DAST_PROVIDER_STALL_TIMEOUT_SECONDS)


def dast_deadline_exhausted(deadline: datetime | None, *, now: datetime | None = None) -> bool:
    """Whether a run has outlived its wall-clock ceiling and its grace window."""
    if deadline is None:
        return False
    return (now or timezone.now()) >= deadline + dast_unreachable_grace()


def dast_progress_stalled(last_progress_at: datetime | None, *, now: datetime | None = None) -> bool:
    """
    Whether the provider has gone quiet for longer than a working run ever does.

    ``last_progress_at`` is when the run last delivered a run id or new log events. None means
    "no baseline", never "stalled".
    """
    stall_timeout = dast_provider_stall_timeout()
    if stall_timeout is None or last_progress_at is None:
        return False
    return (now or timezone.now()) >= last_progress_at + stall_timeout


_FINAL_ATTEMPT_FIELD = "final_attempt_at"


def dast_final_pass_taken(recovery_checkpoint: object) -> bool:
    """Whether this run has already had the one pass it is granted after it is over."""
    if not isinstance(recovery_checkpoint, dict):
        return False
    return bool(recovery_checkpoint.get(_FINAL_ATTEMPT_FIELD))


def dast_mark_final_pass(recovery_checkpoint: object, *, now: datetime | None = None) -> dict:
    """Record the final pass. Must be written before that pass runs, so a crash cannot repeat it."""
    checkpoint = dict(recovery_checkpoint) if isinstance(recovery_checkpoint, dict) else {}
    checkpoint[_FINAL_ATTEMPT_FIELD] = (now or timezone.now()).isoformat()
    return checkpoint


def dast_execution_over(
    *,
    deadline: datetime | None,
    last_progress_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Whether a run may no longer be retried or resumed, for either reason it can be over."""
    observation_time = now or timezone.now()
    return dast_deadline_exhausted(deadline, now=observation_time) or dast_progress_stalled(
        last_progress_at,
        now=observation_time,
    )
