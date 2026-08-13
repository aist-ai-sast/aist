"""
The single answer to "is this DAST run over?".

Retries, reconciliation and cancellation all end a run, and each used to decide on its own.
They ask this module instead, so a run cannot be abandoned by one and resurrected by another.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

_DEFAULT_EXECUTION_TIMEOUT = timedelta(hours=4)
_DEFAULT_UNREACHABLE_GRACE = timedelta(minutes=15)


def dast_execution_timeout() -> timedelta:
    """How long one DAST run may take before its deadline passes."""
    seconds = getattr(settings, "AIST_DAST_EXECUTION_TIMEOUT_SECONDS", None)
    if seconds is None:
        return _DEFAULT_EXECUTION_TIMEOUT
    return timedelta(seconds=int(seconds))


def dast_unreachable_grace() -> timedelta:
    """How long an unreachable provider is retried past the deadline before giving up."""
    seconds = getattr(settings, "AIST_DAST_UNREACHABLE_GRACE_SECONDS", None)
    if seconds is None:
        return _DEFAULT_UNREACHABLE_GRACE
    return timedelta(seconds=int(seconds))


def dast_deadline_exhausted(deadline: datetime | None, *, now: datetime | None = None) -> bool:
    """Whether a run with this deadline may no longer be retried or resumed."""
    if deadline is None:
        return False
    return (now or timezone.now()) >= deadline + dast_unreachable_grace()
