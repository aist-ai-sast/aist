"""
UX-only in-flight marker for bulk finding operations.

This module is NOT a data integrity mechanism. Concurrent write safety is
guaranteed at the database level via ``select_for_update(nowait=True)`` in
the bulk status endpoint.

The cache marker serves two UX purposes:
1. Returns specific locked IDs in the 423 pre-check so the UI can highlight
   exactly which findings are blocked before even touching the DB.
2. Lets ``AistFindingBulkLockMiddleware`` return an early 423 to single-finding
   mutations (PATCH/PUT/DELETE/close) while a bulk operation is in-flight,
   instead of making those requests queue behind the DB row lock.

If the cache backend is unavailable, both UX features degrade gracefully —
the integrity guarantee from ``select_for_update(nowait=True)`` is unaffected.
"""
from __future__ import annotations

from django.core.cache import cache

LOCK_TTL_SECONDS = 180
LOCK_KEY_PREFIX = "aist:finding_bulk_lock"


def _lock_key(finding_id: int) -> str:
    return f"{LOCK_KEY_PREFIX}:{finding_id}"


def normalize_finding_ids(finding_ids: list[int]) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for finding_id in finding_ids:
        finding_int = int(finding_id)
        if finding_int <= 0 or finding_int in seen:
            continue
        seen.add(finding_int)
        normalized.append(finding_int)
    return normalized


def acquire_bulk_locks(
    finding_ids: list[int],
    owner_token: str,
    timeout: int = LOCK_TTL_SECONDS,
) -> tuple[list[int], list[int]]:
    acquired: list[int] = []
    locked: list[int] = []
    for finding_id in normalize_finding_ids(finding_ids):
        try:
            if cache.add(_lock_key(finding_id), owner_token, timeout=timeout):
                acquired.append(finding_id)
            else:
                locked.append(finding_id)
        except Exception:
            # Cache unavailable: treat as acquired so the UX guard does not block the
            # operation. Data integrity is guaranteed at the DB level via select_for_update.
            acquired.append(finding_id)
    return acquired, locked


def release_bulk_locks(finding_ids: list[int], owner_token: str | None = None) -> None:
    for finding_id in normalize_finding_ids(finding_ids):
        key = _lock_key(finding_id)
        if owner_token is None:
            cache.delete(key)
            continue
        current_owner = cache.get(key)
        if current_owner == owner_token:
            cache.delete(key)


def get_locked_finding_ids(finding_ids: list[int]) -> set[int]:
    normalized = normalize_finding_ids(finding_ids)
    if not normalized:
        return set()
    keys = [_lock_key(finding_id) for finding_id in normalized]
    try:
        values = cache.get_many(keys)
    except Exception:
        return set()
    return {
        finding_id
        for finding_id, key in zip(normalized, keys, strict=False)
        if values.get(key)
    }
