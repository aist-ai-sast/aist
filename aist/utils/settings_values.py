"""Validated reads of AIST settings."""
from __future__ import annotations

from django.conf import settings


def positive_int_setting(name: str) -> int:
    """The setting ``name`` as a positive integer; raises ``ValueError`` naming the setting otherwise."""
    value = getattr(settings, name)
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg) from exc
    if value <= 0:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    return value
