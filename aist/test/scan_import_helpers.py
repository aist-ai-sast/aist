"""Import a scan report through the platform's importer, as the SAST upload does."""

from __future__ import annotations

import json
from io import BytesIO
from typing import TYPE_CHECKING

from dojo.tools.generic.parser import GenericParser

from aist.internal_upload import (
    NO_EXCLUSION,
    FindingExclusion,
    ImportConfig,
    RepoParams,
    ensure_engagement,
    import_scan_file_via_default_importer,
)

if TYPE_CHECKING:
    from pathlib import Path

    from dojo.models import Engagement, Product

GENERIC_SCAN_TYPE = GenericParser.ID
NO_REPOSITORY = RepoParams(repo_url="", branch_tag=None, commit_hash=None, scm_type="generic", local_path=None)


def upload_engagement(product: Product, name: str) -> Engagement:
    """The engagement the SAST upload imports a report of `product` into, for a scan with no repository."""
    return ensure_engagement(product, name, NO_REPOSITORY, ImportConfig().engagement_status)


def generic_report(findings: list[dict]) -> str:
    """`findings` as a Generic Findings Import report."""
    return json.dumps({"findings": findings})


def write_generic_report(path: Path, findings: list[dict]) -> None:
    """Write `findings` as a Generic Findings Import report at `path`."""
    path.write_text(generic_report(findings), encoding="utf-8")


def import_report(
    engagement: Engagement,
    report: bytes,
    *,
    report_name: str,
    scan_type: str,
    lead,
    test_title: str | None = None,
    repo_params: RepoParams = NO_REPOSITORY,
    exclusion: FindingExclusion = NO_EXCLUSION,
):
    """Import `report`, a file named `report_name`, into `engagement` and return the Test and its findings."""
    report_file = BytesIO(report)
    report_file.name = report_name
    return import_scan_file_via_default_importer(
        engagement=engagement,
        scan_type=scan_type,
        report_file=report_file,
        test_title=test_title or f"{scan_type} test",
        repo_params=repo_params,
        minimum_severity="Info",
        lead=lead,
        exclusion=exclusion,
    )


def import_generic_report(
    engagement: Engagement,
    findings: list[dict],
    *,
    lead,
    exclusion: FindingExclusion = NO_EXCLUSION,
):
    """Import `findings` as a Generic Findings Import report into `engagement`; return the Test and findings."""
    return import_report(
        engagement,
        generic_report(findings).encode(),
        report_name="report.json",
        scan_type=GENERIC_SCAN_TYPE,
        lead=lead,
        exclusion=exclusion,
    )
