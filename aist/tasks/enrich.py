import logging
import os
from collections import defaultdict
from hashlib import sha256
from math import ceil
from pathlib import Path
from typing import Any

from celery import chain, chord, shared_task
from django.db import transaction
from dojo.models import DojoMeta, Finding, Test

from aist.launch_data import PipelineLaunchData
from aist.link_builder import LinkBuilder
from aist.logging_transport import get_redis, install_pipeline_logging
from aist.models import AISTPipeline, AISTStatus
from aist.utils.pipeline import set_pipeline_status

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
def report_enrich_done(self, result: int, pipeline_id: str, async_user=None):
    redis = get_redis()
    key = f"aist:progress:{pipeline_id}:enrich"
    redis.hincrby(key, "done", 1)
    return result


@shared_task(name="aist.after_upload_enrich_and_watch")
def after_upload_enrich_and_watch(results: list[int],
                                  pipeline_id: str,
                                  test_ids: list[int],
                                  log_level,
                                  async_user=None) -> None:
    from aist.dedupe.evolution import run_evolution_dedup  # noqa: PLC0415
    from aist.tasks.ai import auto_push_to_ai_if_configured  # noqa: PLC0415
    from aist.tasks.regression import detect_regressions_for_pipeline  # noqa: PLC0415

    logger = install_pipeline_logging(pipeline_id, log_level)
    enriched = sum(int(v or 0) for v in results)

    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)

        if test_ids:
            tests = list(Test.objects.filter(id__in=test_ids))
            pipeline.tests.set(tests, clear=True)

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
    ai = PipelineLaunchData(pipeline.launch_data).ai
    if (ai.get("mode") == "AUTO_DEFAULT") and ai.get("filter_snapshot"):
        auto_push_to_ai_if_configured.delay(pipeline_id)


@shared_task(bind=False)
def enrich_finding_task(
    finding_id: int,
    trim_path: str,
    project_version_descriptor: dict[str, Any],
    async_user=None,
) -> int:
    """Enrich a single finding by trimming its file path and attaching a source link."""
    try:
        f = Finding.objects.select_related("test__engagement").get(id=finding_id)
    except Finding.DoesNotExist:
        return 0
    else:
        file_path = f.file_path or ""
        test_id = getattr(f, "test_id", None)

        # Severity exclusion — evaluated before any further work so that excluded
        # findings never reach the link-building stage.
        excluded_severities = project_version_descriptor.get("excluded_severities", [])
        if excluded_severities and f.severity in excluded_severities:
            f.delete()
            return 1

        try:
            if trim_path and file_path.startswith(trim_path):
                tp = trim_path if trim_path.endswith("/") else trim_path + "/"
                f.file_path = file_path.replace(tp, "")
                f.save(update_fields=["file_path"])
                file_path = f.file_path

            linker = LinkBuilder(project_version_descriptor)
            try:
                link = linker.build(file_path)
            except Exception:
                logger.exception(
                    "Failed to build source link for finding enrichment: "
                    "finding_id=%s file_path=%s test_id=%s project_version_descriptor=%s",
                    finding_id,
                    file_path,
                    test_id,
                    project_version_descriptor,
                )
                return 0

            if not link:
                return 0
            acceptable = not linker.contains_excluded_path(link)
            if acceptable:
                try:
                    DojoMeta.objects.update_or_create(
                        finding=f,
                        name="sourcefile_link",
                        defaults={"value": link},
                    )
                except Exception:
                    logger.exception(
                        "Failed to upsert sourcefile_link meta for finding enrichment: "
                        "finding_id=%s file_path=%s test_id=%s project_version_descriptor=%s",
                        finding_id,
                        file_path,
                        test_id,
                        project_version_descriptor,
                    )
                    return 0
            else:
                f.delete()
            return 1  # noqa: TRY300
        except Exception:
            logger.exception(
                "Unexpected finding enrichment error: "
                "finding_id=%s file_path=%s test_id=%s project_version_descriptor=%s",
                finding_id,
                file_path,
                test_id,
                project_version_descriptor,
            )
            return 0


@shared_task(bind=False)
def annotate_line_hash_batch(
    finding_ids: list[int],
    source_root: str,
    async_user=None,
) -> int:
    """
    Compute a content fingerprint (SHA-256 of the vulnerable line) for each
    finding and persist it as DojoMeta(name="aist:lhash").

    Groups findings by file path so each source file is read at most once per
    batch. Must run before enrich_finding_batch within the same chunk — see
    make_enrich_chord for the chain ordering.
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
        full = (
            Path(file_path)
            if Path(file_path).is_absolute()
            else Path(source_root) / file_path
        )
        try:
            lines = full.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for f in file_findings:
            try:
                content = lines[f.line - 1].strip()
            except IndexError:
                continue
            if not content:
                continue
            h = sha256(content.encode()).hexdigest()[:16]
            to_create.append(DojoMeta(name="aist:lhash", value=h, finding_id=f.id))

    if to_create:
        DojoMeta.objects.bulk_create(to_create, ignore_conflicts=True)
    return len(to_create)


@shared_task(bind=False)
def enrich_finding_batch(
    finding_ids: list[int],
    trim_path: str,
    project_version_descriptor: dict[str, Any],
    async_user=None,
) -> int:
    processed = 0
    for fid in finding_ids:
        try:
            processed += int(enrich_finding_task.run(fid, trim_path, project_version_descriptor) or 0)
        except Exception:  # noqa: S112
            continue
    return processed


def make_enrich_chord(*, pipeline_id: str):
    """
    Build a Celery chord that enriches all post-dedup findings for the pipeline.

    Fetches all required parameters directly from the pipeline and its launch_data,
    so callers only need to supply pipeline_id.

    Steps:
      1) Re-fetch finding IDs from DB (reflects post-dedup state).
      2) Split findings into K chunks (K ~= number of active workers).
      3) Initialize Redis progress (total = live finding count, done = 0).
      4) Per chunk: annotate line hashes first, then enrich (sequential within chunk).
         Chunks run in parallel across workers.

    Returns:
        celery.canvas.Signature: A chord (or simple task) signature ready to dispatch.

    """
    pipeline = AISTPipeline.objects.select_related("project__product").get(id=pipeline_id)
    ld = PipelineLaunchData(pipeline.launch_data)
    log_level = ld.log_level
    test_ids = list(pipeline.tests.values_list("id", flat=True))
    trim_path = ld.trim_path
    project_version_descriptor = ld.project_version_descriptor
    source_root = ld.resolve_source_root(pipeline.project.product.name)

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

    # Initialize progress in Redis: UI reads aist:progress:<pipeline_id>:enrich.
    redis = get_redis()
    redis.hset(f"aist:progress:{pipeline_id}:enrich", mapping={"total": total, "done": 0})

    # annotate_line_hash_batch runs first (before enrich trims file_path and
    # potentially deletes excluded-path findings), then enrich runs on the same chunk.
    header = [
        chain(
            annotate_line_hash_batch.si(chunk, source_root),
            enrich_finding_batch.si(chunk, trim_path, project_version_descriptor),
            report_enrich_done.s(pipeline_id),
        )
        for chunk in chunks
    ]
    body = after_upload_enrich_and_watch.s(pipeline_id, test_ids, log_level)
    errback = on_enrich_chord_error.s(pipeline_id=pipeline_id)
    return chord(header, body).on_error(errback)
