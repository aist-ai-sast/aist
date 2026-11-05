import logging
import os
from collections import defaultdict
from math import ceil
from pathlib import Path

from celery import chain, chord, shared_task
from django.db import transaction
from dojo.models import DojoMeta, Finding

from aist.dedupe.evolution import AIST_LHASH_META_NAME, line_fingerprint
from aist.launch_data import PipelineLaunchData
from aist.link_builder import LinkBuilder, store_source_link
from aist.logging_transport import get_redis, install_pipeline_logging
from aist.models import AISTPipeline, AISTStatus
from aist.progress import EnrichProgress
from aist.utils.archive import safe_join
from aist.utils.pipeline import set_pipeline_status
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.defect_dojo.repo_info import checkout_relative_path  # noqa: E402

logger = logging.getLogger(__name__)


@shared_task(name="aist.on_enrich_chord_error")
def on_enrich_chord_error(*args, pipeline_id: str, **kwargs) -> None:
    """
    Celery errback invoked when the enrich chord fails at the infrastructure level
    (e.g. worker crash, task revocation). Transitions the pipeline to a degraded
    terminal state so it does not remain stuck in FINDING_POSTPROCESSING.

    Note: individual finding failures are handled inside enrich_finding_batch and
    never propagate as exceptions, so this handler covers only catastrophic failures.
    """
    from aist.utils.pipeline import finish_pipeline  # noqa: PLC0415

    logger.error(
        "Enrich chord failed for pipeline=%s; marking pipeline as degraded. args=%s",
        pipeline_id,
        args,
    )
    finish_pipeline(pipeline_id, degraded=True)


@shared_task(bind=True)
def report_enrich_done(self, result: int, pipeline_id: str, findings: int, async_user_id=None):
    """Count a finished chunk's ``findings`` towards the pipeline's enrich progress and pass its result on."""
    EnrichProgress.advance(get_redis(), pipeline_id, findings)
    return result


@shared_task(name="aist.after_upload_enrich_and_watch")
def after_upload_enrich_and_watch(results: list[int],
                                  pipeline_id: str,
                                  test_ids: list[int],
                                  log_level,
                                  async_user_id=None) -> None:
    from aist.dedupe.evolution import run_evolution_dedup  # noqa: PLC0415
    from aist.tasks.ai import auto_push_to_ai_if_configured, resolve_triage_type  # noqa: PLC0415
    from aist.tasks.regression import detect_regressions_for_pipeline  # noqa: PLC0415

    logger = install_pipeline_logging(pipeline_id, log_level)
    enriched = sum(int(v or 0) for v in results)

    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
        set_pipeline_status(pipeline, AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI)

    logger.info("Enrichment finished: %s findings enriched.", enriched)

    try:
        run_evolution_dedup(pipeline_id=pipeline_id, test_ids=test_ids, logger=logger)
    except Exception:
        logger.exception("Evolution dedup failed (pipeline_id=%s); continuing.", pipeline_id)

    pipeline = AISTPipeline.objects.get(id=pipeline_id)
    try:
        detect_regressions_for_pipeline(
            pipeline_id=pipeline_id,
            test_ids=list(pipeline.tests.values_list("id", flat=True)),
        )
    except Exception:
        logger.exception("Regression detection failed (pipeline_id=%s); continuing.", pipeline_id)
    if PipelineLaunchData(pipeline.launch_data).ai.pushes_automatically(resolve_triage_type(pipeline)):
        auto_push_to_ai_if_configured.delay(pipeline_id)


def _enrich_finding_link(f, linker: LinkBuilder) -> int:
    file_path = linker.relative_path(f.file_path or "")
    if file_path != (f.file_path or ""):
        f.file_path = file_path
        f.save(update_fields=["file_path"])

    try:
        link = linker.build(file_path)
    except Exception:
        logger.exception(
            "Failed to build source link for finding enrichment: finding_id=%s file_path=%s test_id=%s version_id=%s",
            f.id,
            file_path,
            f.test_id,
            linker.version_id,
        )
        return 0

    if not link:
        return 0
    try:
        store_source_link(f.id, link)
    except Exception:
        logger.exception(
            "Failed to upsert sourcefile_link meta for finding enrichment: "
            "finding_id=%s file_path=%s test_id=%s version_id=%s",
            f.id,
            file_path,
            f.test_id,
            linker.version_id,
        )
        return 0
    return 1


def _enrich_finding(finding_id: int, linker: LinkBuilder) -> int:
    try:
        f = Finding.objects.get(id=finding_id)
    except Finding.DoesNotExist:
        logger.info("Finding %s no longer exists; nothing to enrich.", finding_id)
        return 0
    try:
        return _enrich_finding_link(f, linker)
    except Exception:
        logger.exception(
            "Unexpected finding enrichment error: finding_id=%s file_path=%s test_id=%s version_id=%s",
            finding_id,
            f.file_path,
            f.test_id,
            linker.version_id,
        )
        return 0


@shared_task(bind=False)
def enrich_finding_task(
    finding_id: int,
    trim_path: str,
    project_version_id: int | None,
    async_user_id=None,
) -> int:
    """Enrich a single finding by trimming its file path and attaching a source link into its project version."""
    return _enrich_finding(finding_id, LinkBuilder(project_version_id, trim_path))


def _checkout_source_lines(source_root: Path, file_path: str, trim_path: str) -> list[str] | None:
    """
    The lines of the regular file ``file_path`` names inside the checkout at ``source_root``.

    ``file_path`` is read relative to the scanned checkout root ``trim_path``. None when it resolves outside
    ``source_root``, is not a regular file, or cannot be read.
    """
    try:
        full = safe_join(source_root, checkout_relative_path(file_path, trim_path))
    except ValueError:
        return None
    if not full.is_file():
        return None
    try:
        return full.read_text(errors="replace").splitlines()
    except OSError:
        return None


@shared_task(bind=False)
def annotate_line_hash_batch(
    finding_ids: list[int],
    source_root: str,
    trim_path: str = "",
    async_user_id=None,
) -> int:
    """
    Compute a content fingerprint (SHA-256 of the vulnerable line) for each
    finding and persist it as DojoMeta(name=AIST_LHASH_META_NAME).

    A finding's file is read under ``source_root`` at its path relative to the
    scanned checkout root ``trim_path``. A finding whose file resolves outside
    ``source_root`` or is not a readable regular file gets no fingerprint.

    Groups findings by file path so each source file is read at most once per
    batch.
    """
    if not source_root:
        return 0

    findings = list(
        Finding.objects
        .filter(id__in=finding_ids)
        .only("id", "file_path", "line"),
    )

    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.file_path and f.line:
            by_file[f.file_path].append(f)

    to_create = []
    for file_path, file_findings in by_file.items():
        lines = _checkout_source_lines(Path(source_root), file_path, trim_path)
        if lines is None:
            logger.warning(
                "No line fingerprint for findings %s: their source file is not readable inside the checkout "
                "(file_path=%s)",
                [f.id for f in file_findings],
                file_path,
            )
            continue
        for f in file_findings:
            fingerprint = line_fingerprint(lines, f.line)
            if fingerprint:
                to_create.append(DojoMeta(name=AIST_LHASH_META_NAME, value=fingerprint, finding_id=f.id))

    if to_create:
        DojoMeta.objects.bulk_create(to_create, ignore_conflicts=True)
    return len(to_create)


@shared_task(bind=False)
def enrich_finding_batch(
    finding_ids: list[int],
    trim_path: str,
    project_version_id: int | None,
    async_user_id=None,
) -> int:
    linker = LinkBuilder(project_version_id, trim_path)
    processed = 0
    for finding_id in finding_ids:
        try:
            processed += _enrich_finding(finding_id, linker)
        except Exception:
            logger.exception("Enrichment failed for finding_id=%s; continuing with the batch.", finding_id)
    return processed


def make_enrich_chord(*, pipeline_id: str):
    """
    Build a Celery chord that enriches all post-dedup findings for the pipeline.

    Fetches all required parameters directly from the pipeline and its launch_data,
    so callers only need to supply pipeline_id.

    Steps:
      1) Re-fetch finding IDs from DB (reflects post-dedup state).
      2) Split findings into at most DD_CELERY_WORKER_AUTOSCALE_MAX chunks.
      3) Initialize Redis progress (total = live finding count, done = 0).
      4) Per chunk: annotate line hashes, then enrich (sequential within chunk).
         Chunks run in parallel across workers.

    Returns:
        celery.canvas.Signature: A chord (or simple task) signature ready to dispatch.

    """
    pipeline = AISTPipeline.objects.select_related("project__product", "project_version").get(id=pipeline_id)
    ld = PipelineLaunchData(pipeline.launch_data)
    log_level = ld.log_level
    test_ids = list(pipeline.tests.values_list("id", flat=True))
    trim_path = ld.trim_path
    source_root = pipeline.source_root

    # Re-fetch finding IDs after dedup — duplicates may have been deleted.
    finding_ids = list(Finding.objects.filter(test_id__in=test_ids).values_list("id", flat=True))

    logger = install_pipeline_logging(pipeline_id, log_level)
    total = len(finding_ids)
    logger.info("Enrichment scheduled: %s findings across %s tests", total, len(test_ids))

    # Edge case: no findings after dedup → skip chord, go straight to callback.
    if total == 0:
        return after_upload_enrich_and_watch.si([], pipeline_id, test_ids, log_level)

    workers = int(os.getenv("DD_CELERY_WORKER_AUTOSCALE_MAX", "4") or 4)
    k = max(1, min(workers, total))
    chunk_size = ceil(total / k)
    chunks = [finding_ids[i: i + chunk_size] for i in range(0, total, chunk_size)]

    EnrichProgress.start(get_redis(), pipeline_id, total)

    header = [
        chain(
            annotate_line_hash_batch.si(chunk, source_root, trim_path),
            enrich_finding_batch.si(chunk, trim_path, pipeline.project_version_id),
            report_enrich_done.s(pipeline_id, len(chunk)),
        )
        for chunk in chunks
    ]
    body = after_upload_enrich_and_watch.s(pipeline_id, test_ids, log_level)
    errback = on_enrich_chord_error.s(pipeline_id=pipeline_id)
    return chord(header, body).on_error(errback)
