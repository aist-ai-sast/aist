from __future__ import annotations

import csv
from io import StringIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dojo.aist.models import AISTPipeline


def _build_ai_export_rows(
    pipeline: AISTPipeline,
    ai_payload: dict,
    ignore_false_positives,
) -> list[dict]:
    """
    Normalize AI payload into a flat list of findings suitable for tabular export.

    The function:
    - merges all list-like collections from payload["results"] (e.g. true_positives, uncertainly)
    - maps nested originalFinding.* fields into flat columns
    - optionally filters out items with falsePositive == True
    - keeps impactScore in each row for sorting, but does not expose it as a visible column
    """
    results = ai_payload.get("results") or {}
    findings_raw: list[dict] = []

    if isinstance(results, dict):
        for value in results.values():
            if isinstance(value, list):
                findings_raw.extend([item for item in value if isinstance(item, dict)])
    elif isinstance(results, list):
        findings_raw = [item for item in results if isinstance(item, dict)]

    rows: list[dict] = []
    for item in findings_raw:
        original = item.get("originalFinding") or {}

        # Project version is expected to come from AI response when available;
        # we fall back to the pipeline's project_version label.
        project_version_label = pipeline.resolved_commit or pipeline.project_version.version

        row = {
            "title": item.get("title") or "",
            "project_version": project_version_label,
            "cwe": original.get("cwe") or "",
            "file": original.get("file") or "",
            "line": original.get("line") or "",
            # "description" is taken from AI explanation; adjust if your schema differs.
            "description": item.get("reasoning") or "",
            # "code_snippet" is taken from originalFinding.snippet when available.
            "code_snippet": original.get("snippet") or "",
            "false_positive": bool(item.get("falsePositive")),
            "impactScore": item.get("impactScore") or 0,
        }

        if ignore_false_positives and row["false_positive"]:
            continue

        rows.append(row)

    # Sort by impactScore descending: highest impact first.
    rows.sort(key=lambda r: r.get("impactScore") or 0, reverse=True)
    return rows


def build_ai_export_csv_text(
    pipeline: AISTPipeline,
    *,
    payload: dict | None = None,
    ignore_false_positives: bool = False,
    columns: list[str] | None = None,
) -> str:
    if payload is None:
        ai_response = (
            pipeline.ai_responses
            .order_by("-created")
            .first()
        )
        if not ai_response or not ai_response.payload:
            return ""
        payload = ai_response.payload or {}

    selected_columns = columns or [
        "title",
        "project_version",
        "cwe",
        "file",
        "line",
        "description",
        "code_snippet",
        "false_positive",
    ]

    header_map = {
        "title": "Title",
        "project_version": "Project version",
        "cwe": "CWE",
        "file": "File",
        "line": "Line",
        "description": "Description",
        "code_snippet": "Code snippet",
        "false_positive": "False positive",
    }

    rows = _build_ai_export_rows(pipeline, payload, ignore_false_positives=ignore_false_positives)
    if not rows:
        return ""

    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow([header_map[c] for c in selected_columns])
    for row in rows:
        writer.writerow([row.get(c, "") for c in selected_columns])
    return buffer.getvalue()
