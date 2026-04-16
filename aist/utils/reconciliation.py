from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from dojo.models import DojoMeta, Finding, Test

from aist.launch_data import PipelineLaunchData
from aist.link_builder import LinkBuilder
from aist.models import AISTPipeline, AISTProjectVersion, VersionType

LOGGER = logging.getLogger(__name__)
SOURCEFILE_LINK_META_NAME = "sourcefile_link"
ATTACH_CHUNK_SIZE = 500


@dataclass
class AttachStats:
    requested: int = 0
    missing_before_insert: int = 0
    missing_on_retry: int = 0
    linked: int = 0
    integrity_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def log(self, *, logger, pv_id: int, label: str) -> None:
        logger.info(
            "%s attach stats (pv_id=%s): requested=%s dropped_before=%s dropped_on_retry=%s linked=%s",
            label,
            pv_id,
            self.requested,
            self.missing_before_insert,
            self.missing_on_retry,
            self.linked,
        )


@dataclass
class SourceLinkStats:
    missing_before_fix: int = 0
    excluded: int = 0
    eligible: int = 0
    remaining_missing: int = 0


@dataclass
class ReconciliationStats:
    pipeline_id: str
    dry_run: bool
    pipeline_missing: bool = False
    imported_tests: int = 0
    missing_pipeline_test_links: int = 0
    fixed_pipeline_test_links: int = 0
    findings_in_imported_tests: int = 0
    missing_project_version_links: int = 0
    pv_attach: AttachStats = field(default_factory=AttachStats)
    parent_attach: AttachStats = field(default_factory=AttachStats)
    source_links: SourceLinkStats = field(default_factory=SourceLinkStats)
    remaining_missing_pipeline_test_links: int = 0
    remaining_missing_project_version_links: int = 0

    @property
    def remaining_violations(self) -> int:
        return (
            self.remaining_missing_pipeline_test_links
            + self.remaining_missing_project_version_links
            + self.source_links.remaining_missing
        )

    def as_dict(self) -> dict:
        return {
            "pipeline_id": self.pipeline_id,
            "dry_run": self.dry_run,
            "pipeline_missing": self.pipeline_missing,
            "imported_tests": self.imported_tests,
            "missing_pipeline_test_links": self.missing_pipeline_test_links,
            "fixed_pipeline_test_links": self.fixed_pipeline_test_links,
            "findings_in_imported_tests": self.findings_in_imported_tests,
            "missing_project_version_links": self.missing_project_version_links,
            "pv_attach": self.pv_attach.as_dict(),
            "parent_attach": self.parent_attach.as_dict(),
            "missing_sourcefile_link": self.source_links.missing_before_fix,
            "excluded_sourcefile_link": self.source_links.excluded,
            "remaining_missing_pipeline_test_links": self.remaining_missing_pipeline_test_links,
            "remaining_missing_project_version_links": self.remaining_missing_project_version_links,
            "remaining_sourcefile_link_violations": self.source_links.remaining_missing,
            "remaining_violations": self.remaining_violations,
        }


def _chunked(values: list[int], size: int = ATTACH_CHUNK_SIZE):
    for idx in range(0, len(values), size):
        yield values[idx: idx + size]


def _normalize_ids(raw_ids) -> list[int]:
    normalized: list[int] = []
    for value in raw_ids or []:
        try:
            normalized.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(normalized))


def _build_version_descriptor(pipeline: AISTPipeline) -> dict:
    pv = pipeline.project_version
    project = pipeline.project if pipeline.project_id else None
    return {
        "id": pv.id if pv else "",
        "type": pv.version_type if pv else VersionType.FILE_HASH,
        "excluded_paths": project.get_excluded_paths() if project else [],
        "excluded_severities": project.get_excluded_severities() if project else [],
    }


def _get_pipeline(pipeline_id: str) -> AISTPipeline | None:
    return (
        AISTPipeline.objects
        .select_related("project", "project_version", "project_version__resolved_from_branch")
        .filter(id=pipeline_id)
        .first()
    )


def _get_imported_test_ids(pipeline: AISTPipeline) -> list[int]:
    imported_ids = _normalize_ids(PipelineLaunchData(pipeline.launch_data).imported_test_ids)
    if imported_ids:
        return imported_ids
    return sorted(set(pipeline.tests.values_list("id", flat=True)))


def _findings_for_tests(test_ids: list[int]):
    if not test_ids:
        return Finding.objects.none()
    return Finding.objects.filter(test_id__in=test_ids)


def _find_findings_without_any_project_version(finding_ids: list[int]) -> list[int]:
    return list(
        Finding.objects.filter(id__in=finding_ids).filter(
            ~Exists(
                AISTProjectVersion.findings.through.objects.filter(finding_id=OuterRef("id")),
            ),
        ).values_list("id", flat=True),
    )


def _link_missing_pipeline_tests(
    *,
    pipeline: AISTPipeline,
    imported_ids: list[int],
    dry_run: bool,
) -> tuple[list[int], int]:
    imported_set = set(imported_ids)
    current_pipeline_tests = set(pipeline.tests.values_list("id", flat=True))
    missing_ids = sorted(imported_set - current_pipeline_tests)
    fixed_count = 0
    if missing_ids and not dry_run:
        tests_to_add = list(Test.objects.filter(id__in=missing_ids))
        if tests_to_add:
            pipeline.tests.add(*tests_to_add)
            fixed_count = len(tests_to_add)
    return missing_ids, fixed_count


def _attach_missing_findings_to_pipeline_versions(
    *,
    pipeline: AISTPipeline,
    missing_finding_ids: list[int],
    dry_run: bool,
    logger,
) -> tuple[AttachStats, AttachStats]:
    pv_attach_stats = AttachStats(requested=len(missing_finding_ids))
    parent_attach_stats = AttachStats()

    if dry_run or not missing_finding_ids or not pipeline.project_version_id:
        return pv_attach_stats, parent_attach_stats

    with transaction.atomic():
        pv = AISTProjectVersion.objects.select_for_update().get(id=pipeline.project_version_id)
        pv_attach_stats = safe_attach_findings_to_version(
            pv=pv,
            finding_ids=missing_finding_ids,
            logger=logger,
        )
        if pv.version_type == VersionType.GIT_HASH and pv.resolved_from_branch_id:
            parent = AISTProjectVersion.objects.select_for_update().get(id=pv.resolved_from_branch_id)
            parent_attach_stats = safe_attach_findings_to_version(
                pv=parent,
                finding_ids=missing_finding_ids,
                logger=logger,
            )
    return pv_attach_stats, parent_attach_stats


def _collect_source_link_candidates(findings_qs, linker: LinkBuilder) -> tuple[list[tuple[int, str]], int]:
    candidates = findings_qs.exclude(file_path__isnull=True).exclude(file_path="")
    eligible: list[tuple[int, str]] = []
    excluded_count = 0
    for finding in candidates.iterator():
        link = linker.build(finding.file_path or "")
        if not link:
            continue
        if linker.contains_excluded_path(link):
            excluded_count += 1
            continue
        eligible.append((finding.id, link))
    return eligible, excluded_count


def _reconcile_source_links(
    *,
    findings_qs,
    pipeline: AISTPipeline,
    dry_run: bool,
) -> SourceLinkStats:
    linker = LinkBuilder(_build_version_descriptor(pipeline))
    eligible_candidates, excluded_count = _collect_source_link_candidates(findings_qs, linker)
    eligible_ids = [fid for fid, _ in eligible_candidates]

    missing_before_fix = 0
    for finding_id, link in eligible_candidates:
        has_meta = DojoMeta.objects.filter(finding_id=finding_id, name=SOURCEFILE_LINK_META_NAME).exists()
        if has_meta:
            continue
        missing_before_fix += 1
        if dry_run:
            continue
        DojoMeta.objects.update_or_create(
            finding_id=finding_id,
            name=SOURCEFILE_LINK_META_NAME,
            defaults={"value": link},
        )

    existing_link_ids = set(
        DojoMeta.objects.filter(
            finding_id__in=eligible_ids,
            name=SOURCEFILE_LINK_META_NAME,
        ).values_list("finding_id", flat=True),
    )
    remaining_missing = max(len(eligible_ids) - len(existing_link_ids), 0)
    return SourceLinkStats(
        missing_before_fix=missing_before_fix,
        excluded=excluded_count,
        eligible=len(eligible_ids),
        remaining_missing=remaining_missing,
    )


def safe_attach_findings_to_version(
    *,
    pv: AISTProjectVersion,
    finding_ids: list[int],
    logger,
) -> AttachStats:
    stats = AttachStats(requested=len(finding_ids))
    if not finding_ids:
        return stats

    through_model = pv.findings.through
    # SELECT FOR UPDATE locks findings against concurrent DELETE.  Django creates FK
    # constraints as DEFERRABLE INITIALLY DEFERRED, so the FK check happens at COMMIT,
    # not at INSERT.  Without this lock, a concurrent process (e.g. deduplication) can
    # delete a finding between our INSERT and our COMMIT, causing a FK violation at
    # COMMIT time.  The lock is held only for the duration of the INSERT, which is fast.
    locked_ids = set(
        Finding.objects.select_for_update()
        .filter(id__in=finding_ids)
        .values_list("id", flat=True),
    )
    stats.missing_before_insert = len(finding_ids) - len(locked_ids)
    if not locked_ids:
        return stats

    after_count = through_model.objects.filter(
        aistprojectversion_id=pv.id,
        finding_id__in=list(locked_ids),
    ).count()
    before_count = after_count

    through_table = through_model._meta.db_table
    finding_table = Finding._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {through_table} (aistprojectversion_id, finding_id)
            SELECT %s, src.finding_id
            FROM UNNEST(%s::bigint[]) AS src(finding_id)
            INNER JOIN {finding_table} f ON f.id = src.finding_id
            ON CONFLICT DO NOTHING
            """,
            [pv.id, list(locked_ids)],
        )

    after_count = through_model.objects.filter(
        aistprojectversion_id=pv.id,
        finding_id__in=list(locked_ids),
    ).count()
    stats.linked = max(after_count - before_count, 0)
    return stats


def reconcile_pipeline_orphans(
    *,
    pipeline_id: str,
    dry_run: bool = False,
    logger=None,
) -> dict:
    logger = logger or LOGGER
    pipeline = _get_pipeline(pipeline_id)
    if not pipeline:
        return ReconciliationStats(
            pipeline_id=pipeline_id,
            dry_run=dry_run,
            pipeline_missing=True,
            remaining_missing_pipeline_test_links=1,
        ).as_dict()

    imported_ids = _get_imported_test_ids(pipeline)
    missing_pipeline_links, fixed_pipeline_links = _link_missing_pipeline_tests(
        pipeline=pipeline,
        imported_ids=imported_ids,
        dry_run=dry_run,
    )

    findings_qs = _findings_for_tests(imported_ids)
    finding_ids = list(findings_qs.values_list("id", flat=True))
    missing_any_pv = _find_findings_without_any_project_version(finding_ids)

    pv_attach_stats, parent_attach_stats = _attach_missing_findings_to_pipeline_versions(
        pipeline=pipeline,
        missing_finding_ids=missing_any_pv,
        dry_run=dry_run,
        logger=logger,
    )
    source_link_stats = _reconcile_source_links(
        findings_qs=findings_qs,
        pipeline=pipeline,
        dry_run=dry_run,
    )

    remaining_missing_pipeline_links = len(
        sorted(set(imported_ids) - set(pipeline.tests.values_list("id", flat=True))),
    )
    remaining_missing_pv = _find_findings_without_any_project_version(finding_ids)

    return ReconciliationStats(
        pipeline_id=pipeline.id,
        dry_run=dry_run,
        imported_tests=len(set(imported_ids)),
        missing_pipeline_test_links=len(missing_pipeline_links),
        fixed_pipeline_test_links=fixed_pipeline_links,
        findings_in_imported_tests=len(finding_ids),
        missing_project_version_links=len(missing_any_pv),
        pv_attach=pv_attach_stats,
        parent_attach=parent_attach_stats,
        source_links=source_link_stats,
        remaining_missing_pipeline_test_links=remaining_missing_pipeline_links,
        remaining_missing_project_version_links=len(remaining_missing_pv),
    ).as_dict()


def _collect_recent_pipeline_ids(*, hours: int, batch_size: int) -> list[str]:
    now = timezone.now()
    since = now - timedelta(hours=max(int(hours or 1), 1))
    return list(
        AISTPipeline.objects
        .filter(created__gte=since)
        .order_by("-created")
        .values_list("id", flat=True)[: max(int(batch_size or 1), 1)],
    )


def reconcile_recent_pipelines(
    *,
    hours: int = 24,
    batch_size: int = 200,
    dry_run: bool = False,
    logger=None,
) -> dict:
    logger = logger or LOGGER
    pipelines = _collect_recent_pipeline_ids(hours=hours, batch_size=batch_size)

    processed = 0
    with_warnings = 0
    violations = 0
    for pipeline_id in pipelines:
        stats = reconcile_pipeline_orphans(pipeline_id=pipeline_id, dry_run=dry_run, logger=logger)
        processed += 1
        violations += int(stats.get("remaining_violations") or 0)
        if stats.get("remaining_violations"):
            with_warnings += 1

    return {
        "dry_run": dry_run,
        "hours": max(int(hours or 1), 1),
        "processed": processed,
        "pipelines_with_remaining_violations": with_warnings,
        "remaining_violations": violations,
    }
