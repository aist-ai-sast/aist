from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

from django.contrib.auth import get_user_model
from django.utils import timezone
from dojo.importers.default_importer import DefaultImporter
from dojo.importers.services import import_scan
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Test,
)
from dojo.user.models import DEDUPLICATION_EXECUTION_MODE_ASYNC

from aist.profile import ProjectProfile
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

# These imports intentionally come AFTER the call above; suppress E402 explicitly.
from pipeline.config_utils import AnalyzersConfigHelper  # noqa: E402
from pipeline.defect_dojo.repo_info import RepoParams, checkout_relative_path, read_repo_params  # noqa: E402


@dataclass
class ImportConfig:

    """
    Configuration options loaded from the defectdojo.yaml file.

    Only fields relevant to the internal importer are defined here. The
    original configuration includes other options (URL, SSL verification)
    which are only applicable when using the REST API. These are
    intentionally omitted.
    """

    minimum_severity: str = "Info"
    name_mode: str = "analyzer-sha"
    engagement_status: str = "In Progress"


class ImportFailure(StrEnum):

    """Why an analyzer's report produced no Test."""

    IMPORT_FAILED = "import_failed"


@dataclass(frozen=True)
class ImportResult:

    """What importing one configured analyzer's report produced: its Test, or the reason it produced none."""

    analyzer_name: str
    test_id: int | None = None
    engagement_id: int | None = None
    imported_findings: int = 0
    failure_code: ImportFailure | None = None
    failure_message: str = ""


def derive_engagement_name(analyzer_name: str, branch: str | None, commit: str | None, name_mode: str) -> str:
    """
    Derive an engagement name from analyser and repository information.

    The naming scheme mirrors the logic in :meth:`SastPipelineDDClient.derive_engagement_name`.

    :param analyzer_name: Name of the analyser/tool used to produce the report.
    :param branch: Current Git branch or tag if available.
    :param commit: Full commit hash if available.
    :param name_mode: Naming mode configured in defectdojo.yaml.
    :returns: A string suitable for use as an Engagement name.
    """
    short_sha = (commit or "")[:8] if commit else None
    if name_mode == "analyzer":
        return analyzer_name
    if name_mode == "analyzer-branch":
        return "-".join([x for x in [analyzer_name, branch] if x])
    # Default: analyzer-sha
    return "-".join([x for x in [analyzer_name, short_sha] if x]) or analyzer_name


def ensure_engagement(product: Product, name: str, repo_params: RepoParams, status: str) -> Engagement:
    """
    Get or create an Engagement with repository metadata.

    An existing engagement of that name takes the given repository metadata
    (source_code_management_uri, branch_tag, commit_hash) and status where they differ.
    Otherwise a new engagement is created spanning today to tomorrow with the given status.
    """
    engagement = Engagement.objects.filter(product=product, name=name).first()
    if engagement:
        updated = False
        if repo_params.repo_url and engagement.source_code_management_uri != repo_params.repo_url:
            engagement.source_code_management_uri = repo_params.repo_url
            updated = True
        if repo_params.branch_tag and engagement.branch_tag != repo_params.branch_tag:
            engagement.branch_tag = repo_params.branch_tag
            updated = True
        if repo_params.commit_hash and engagement.commit_hash != repo_params.commit_hash:
            engagement.commit_hash = repo_params.commit_hash
            updated = True
        if status and engagement.status != status:
            engagement.status = status
            updated = True
        if updated:
            engagement.save(update_fields=["source_code_management_uri", "branch_tag", "commit_hash", "status"])
        return engagement
    # Create new engagement
    today = date.today()
    tomorrow = today + timedelta(days=1)
    return Engagement.objects.create(
        name=name,
        product=product,
        engagement_type="CI/CD",
        target_start=today,
        target_end=tomorrow,
        status=status,
        source_code_management_uri=repo_params.repo_url or None,
        branch_tag=repo_params.branch_tag,
        commit_hash=repo_params.commit_hash,
    )


@dataclass(frozen=True)
class FindingExclusion:

    """
    The findings a project profile keeps out of one scan import.

    Path patterns match the finding's path relative to the checkout root, written with a leading ``/`` so a
    pattern may anchor at the root.
    """

    profile: ProjectProfile = field(default_factory=ProjectProfile)
    checkout_root: str = ""

    def excludes(self, file_path: str, severity: str) -> bool:
        rooted_path = "/" + checkout_relative_path(file_path, self.checkout_root).lstrip("/")
        return self.profile.excludes_finding(rooted_path, severity)


NO_EXCLUSION = FindingExclusion()


class ProfileExcludingImporter(DefaultImporter):

    """DefaultImporter that leaves out every parsed finding `exclusion` excludes, before any is saved."""

    def __init__(self, *, exclusion: FindingExclusion, **kwargs):
        super().__init__(**kwargs)
        self.exclusion = exclusion

    def process_findings(self, parsed_findings: list[Finding], **kwargs) -> list[Finding]:
        kept = [
            finding
            for finding in parsed_findings or []
            if not self.exclusion.excludes(finding.file_path or "", self.sanitize_severity(finding).severity)
        ]
        return super().process_findings(kept, **kwargs)


def import_scan_via_default_importer(
    engagement: Engagement,
    scan_type: str,
    report_path: Path,
    test_title: str,
    repo_params: RepoParams,
    minimum_severity: str,
    lead=None,
    exclusion: FindingExclusion = NO_EXCLUSION,
) -> tuple[Test, list[Finding]]:
    """Import the report at `report_path` through :func:`import_scan_file_via_default_importer`."""
    with Path(report_path).open("rb") as report_file:
        return import_scan_file_via_default_importer(
            engagement=engagement,
            scan_type=scan_type,
            report_file=report_file,
            test_title=test_title,
            repo_params=repo_params,
            minimum_severity=minimum_severity,
            lead=lead,
            exclusion=exclusion,
        )


def import_scan_file_via_default_importer(
    engagement: Engagement,
    scan_type: str,
    report_file,
    test_title: str,
    repo_params: RepoParams,
    minimum_severity: str,
    lead=None,
    exclusion: FindingExclusion = NO_EXCLUSION,
) -> tuple[Test, list[Finding]]:
    """
    Import an open report stream into a new Test and return it with its saved findings.

    The platform's import service runs :class:`ProfileExcludingImporter`, so a finding ``exclusion`` excludes is
    never saved. Deduplication always runs asynchronously: the pipeline watches it, whatever the lead prefers.
    """
    result = import_scan(
        importer_class=ProfileExcludingImporter,
        exclusion=exclusion,
        user=lead,
        lead=lead,
        engagement=engagement,
        scan_file=report_file,
        scan_type=scan_type,
        test_title=test_title,
        scan_date=timezone.now(),
        environment="Development",
        auto_create_context=True,
        minimum_severity=minimum_severity,
        active=True,
        verified=False,
        deduplication_execution_mode=DEDUPLICATION_EXECUTION_MODE_ASYNC,
        branch_tag=repo_params.branch_tag,
        build_id=repo_params.commit_hash or None,
        commit_hash=repo_params.commit_hash,
        create_finding_groups_for_all_findings=False,
    )
    return result.test, list(Finding.objects.filter(test=result.test))


def upload_report_internal(
    analyzer_name: str,
    product: Product,
    scan_type: str,
    report_path: Path,
    repo_params: RepoParams,
    cfg: ImportConfig,
    exclusion: FindingExclusion,
    logger,
    lead=None,
) -> ImportResult:
    """Import one analyzer's report into its engagement of ``product``, leaving out the findings ``exclusion`` excludes."""
    eng_name = derive_engagement_name(analyzer_name, repo_params.branch_tag, repo_params.commit_hash, cfg.name_mode)
    engagement = ensure_engagement(product, eng_name, repo_params, cfg.engagement_status)
    logger.info("Using engagement '%s' (id=%s)", eng_name, engagement.id)
    test_obj, findings = import_scan_via_default_importer(
        engagement=engagement,
        scan_type=scan_type,
        report_path=report_path,
        test_title=f"{analyzer_name} {scan_type}",
        repo_params=repo_params,
        minimum_severity=cfg.minimum_severity,
        lead=lead,
        exclusion=exclusion,
    )
    logger.info("Import finished for %s, %d findings", analyzer_name, len(findings))
    return ImportResult(
        analyzer_name=analyzer_name,
        test_id=test_obj.id,
        engagement_id=engagement.id,
        imported_findings=len(findings),
    )


def upload_results_internal(
    analyzers_cfg_path: str,
    output_dir: str,
    product: Product,
    repo_path: str,
    exclusion: FindingExclusion,
    logger,
) -> list[ImportResult]:
    """
    Import every analyzer report sast-pipeline produced into ``product``.

    A report the platform fails to import is a failed result.
    """
    cfg = ImportConfig()
    repo_dir = repo_path
    try:
        repo_params = read_repo_params(repo_dir)
    except Exception as exc:
        logger.warning(
            "Failed to read repository info from '%s': %s. "
            "Findings will be imported without commit/branch metadata.",
            repo_dir,
            exc,
        )
        repo_params = RepoParams(commit_hash=None, branch_tag=None, repo_url=None, scm_type=None, local_path=repo_dir)

    analyzers_cfg = AnalyzersConfigHelper(analyzers_cfg_path)

    User = get_user_model()
    lead = User.objects.order_by("id").first()
    if lead is None:
        msg = "No users in the database; cannot import findings (lead required)"
        raise RuntimeError(msg)

    results: list[ImportResult] = []
    # Whether an analyzer produced a result, and whether its absence degrades the run, is sast-pipeline's call.
    for report in analyzers_cfg.get_produced_reports(output_dir):
        report_path = report.path_in(output_dir)
        logger.info(
            "Processing report: %s (analyzer=%s, scan_type=%s)", report_path, report.analyzer_name, report.scan_type,
        )
        try:
            results.append(upload_report_internal(
                analyzer_name=report.analyzer_name,
                product=product,
                scan_type=report.scan_type,
                report_path=report_path,
                repo_params=repo_params,
                cfg=cfg,
                exclusion=exclusion,
                logger=logger,
                lead=lead,
            ))
        except Exception as exc:
            logger.exception("Error during uploading report %s", report_path)
            results.append(ImportResult(
                analyzer_name=report.analyzer_name,
                failure_code=ImportFailure.IMPORT_FAILED,
                failure_message=f"Report import failed: {type(exc).__name__}",
            ))
    return results
