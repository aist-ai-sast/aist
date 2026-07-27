from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aist.execution.contracts import PipelineExecutionKind


COALESCE_SCHEMA_VERSION = 1


def canonical_coalesce_key(
    *,
    execution_type: PipelineExecutionKind,
    project_id: int,
    executor_identity: Mapping[str, object],
    params_snapshot: Mapping[str, object],
    capability_snapshot: Mapping[str, object],
) -> str:
    """Hash a versioned, trusted executor identity and its immutable launch snapshots."""
    payload = {
        "schema_version": COALESCE_SCHEMA_VERSION,
        "execution_type": str(execution_type),
        "project_id": project_id,
        "executor_identity": dict(executor_identity),
        "params_snapshot": dict(params_snapshot),
        "capability_snapshot": dict(capability_snapshot),
    }
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"{str(execution_type).lower()}:v{COALESCE_SCHEMA_VERSION}:{digest}"
