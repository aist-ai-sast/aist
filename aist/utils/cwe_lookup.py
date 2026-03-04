"""
Shared CWE metadata lookup utility.

Resolves CWE name, description, and impact from three sources in priority order:
  1. cwe2 library (most complete: full MITRE database)
  2. DefectDojo CWE model (dojo_cwe table)
  3. Vendor CWE fixture (vendor/defectdojo/dojo/fixtures/cwe.json)

All results are cached in-process via lru_cache on the raw data loading,
and per-CWE in Django's cache layer when called via _build_cwe_distribution.
"""
from __future__ import annotations

import contextlib
import csv
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CWE_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "vendor/defectdojo/dojo/fixtures/cwe.json"


def trim_text(value: str | None, *, max_length: int) -> str:
    text = (value or "").strip()
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 1].rstrip()}…"


@lru_cache(maxsize=1)
def load_cwe_fixture_lookup() -> dict[int, dict[str, str]]:
    """Load CWE title+url from vendor fixture (static data, cached forever)."""
    try:
        payload = json.loads(_CWE_FIXTURE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    lookup: dict[int, dict[str, str]] = {}
    if not isinstance(payload, list):
        return lookup
    for item in payload:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields")
        if not isinstance(fields, dict):
            continue
        try:
            number = int(fields.get("number"))
        except (TypeError, ValueError):
            continue
        lookup[number] = {
            "title": str(fields.get("description") or "").strip(),
            "url": str(fields.get("url") or "").strip(),
        }
    return lookup


@lru_cache(maxsize=1)
def _get_cwe2_database():
    try:
        from cwe2.database import Database  # noqa: PLC0415
    except Exception:
        return None
    with contextlib.suppress(Exception):
        return Database()
    return None


@lru_cache(maxsize=1)
def _get_cwe2_row_lookup() -> dict[int, dict[str, str]]:
    db = _get_cwe2_database()
    if db is None:
        return {}
    lookup: dict[int, dict[str, str]] = {}
    with contextlib.suppress(Exception):
        for cwe_file in getattr(db, "cwe_files", []):
            cwe_file.seek(0)
            for row in csv.DictReader(cwe_file):
                raw_id = row.get("CWE-ID")
                if not raw_id:
                    continue
                with contextlib.suppress(ValueError):
                    lookup[int(raw_id)] = row
    return lookup


def _extract_cwe2_impact(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    cleaned = raw.replace("::", " ").replace(":IMPACT:", " ").replace(":SCOPE:", " ")
    cleaned = " ".join(cleaned.split())
    return trim_text(cleaned, max_length=320)


def fetch_cwe_meta(cwe_id: int) -> dict[str, str] | None:
    """
    Return CWE metadata dict with keys: title, description, impact, url.

    Returns None when the CWE is not found in any source.
    Falls back to the vendor fixture if cwe2 is unavailable.
    """
    row = _get_cwe2_row_lookup().get(int(cwe_id))
    if row:
        title = trim_text(str(row.get("Name", "") or ""), max_length=160)
        description = trim_text(
            str(row.get("Description", "") or row.get("Extended Description", "") or ""),
            max_length=320,
        )
        impact = _extract_cwe2_impact(row.get("Common Consequences", ""))
        return {
            "title": title,
            "description": description,
            "impact": impact,
            "url": f"https://cwe.mitre.org/data/definitions/{int(cwe_id)}.html",
        }

    # Fallback: vendor fixture
    local = load_cwe_fixture_lookup().get(int(cwe_id), {})
    if not local:
        return None
    return {
        "title": trim_text(str(local.get("title", "")), max_length=160),
        "description": trim_text(str(local.get("title", "")), max_length=320),
        "impact": "",
        "url": str(local.get("url", "") or f"https://cwe.mitre.org/data/definitions/{int(cwe_id)}.html"),
    }
