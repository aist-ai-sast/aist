import time
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import OperationalError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from dojo.finding.deduplication import do_dedupe_batch_task
from dojo.models import Finding, Test
from dojo.tasks import async_dupe_delete

from aist.logging_transport import install_pipeline_logging
from aist.models import (
    AISTPipeline,
    AISTStatus,
    AISTTestMeta,
    ProcessedFinding,
    TestDeduplicationProgress,
)
from aist.tasks.ai import auto_push_to_ai_if_configured
from aist.utils.pipeline import finish_pipeline, is_terminal_pipeline_status, set_pipeline_status

DEDUP_POLL_SLEEP_S = getattr(settings, "AIST_DEDUP_POLL_SLEEP_S", 3)
DEDUP_STALE_TIMEOUT_S = getattr(settings, "AIST_DEDUP_STALE_TIMEOUT_S", 600)
DEDUP_RECONCILE_INTERVAL_S = getattr(settings, "AIST_DEDUP_RECONCILE_INTERVAL_S", 300)
DEDUP_RECONCILE_BATCH_SIZE = getattr(settings, "AIST_DEDUP_RECONCILE_BATCH_SIZE", 200)
DEDUP_RECONCILE_MAX_ATTEMPTS = getattr(settings, "AIST_DEDUP_RECONCILE_MAX_ATTEMPTS", 3)
DEDUP_MAX_WAIT_S = getattr(settings, "AIST_DEDUP_MAX_WAIT_S", 3600)
DEDUP_DUPLICATE_DELETE_TRIGGER_INTERVAL_S = getattr(
    settings,
    "AIST_DEDUP_DUPLICATE_DELETE_TRIGGER_INTERVAL_S",
    60,
)


def _chunked_ids(iterator, batch_size: int):
    batch = []
    for item in iterator:
        batch.append(item)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _dedup_deadlines(now):
    """Return time thresholds used for stale/reconcile/max-wait decisions."""
    return {
        "stale": now - timedelta(seconds=DEDUP_STALE_TIMEOUT_S),
        "reconcile": now - timedelta(seconds=DEDUP_RECONCILE_INTERVAL_S),
        "max_wait": now - timedelta(seconds=DEDUP_MAX_WAIT_S),
    }


def _progress_is_stale(prog: TestDeduplicationProgress, *, stale_deadline, reconcile_deadline) -> bool:
    return not (
        (prog.last_progress_at and prog.last_progress_at >= stale_deadline)
        or (prog.last_reconcile_at and prog.last_reconcile_at > reconcile_deadline)
    )


def _requeue_missing_findings(*, test_id: int, batch_size: int, logger) -> int:
    missing_qs = (
        Finding.objects
        .filter(test_id=test_id)
        .filter(
            ~Exists(
                ProcessedFinding.objects.filter(
                    test_id=test_id,
                    finding_id=OuterRef("id"),
                ),
            ),
        )
        .values_list("id", flat=True)
    )
    total = 0
    for batch in _chunked_ids(missing_qs.iterator(), batch_size):
        if not batch:
            continue
        do_dedupe_batch_task(batch)
        total += len(batch)
    if total:
        logger.info("Requeued %s findings for deduplication (test_id=%s)", total, test_id)
    return total


def _pipeline_ai_config(pipeline: AISTPipeline) -> dict:
    return (pipeline.launch_data or {}).get("ai") or {}


def _release_pipeline_after_dedup(pipeline: AISTPipeline) -> None:
    set_pipeline_status(pipeline, AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI)
    ai = _pipeline_ai_config(pipeline)
    if (ai.get("mode") == "AUTO_DEFAULT") and ai.get("filter_snapshot"):
        auto_push_to_ai_if_configured.delay(pipeline.id)


def _ensure_dedup_tracking_rows(test_ids: list[int]) -> None:
    if not test_ids:
        return
    existing = set(
        TestDeduplicationProgress.objects
        .filter(test_id__in=test_ids)
        .values_list("test_id", flat=True),
    )
    missing_ids = [tid for tid in test_ids if tid not in existing]
    if missing_ids:
        now = timezone.now()
        TestDeduplicationProgress.objects.bulk_create(
            [
                TestDeduplicationProgress(
                    test_id=tid,
                    started_at=now,
                    last_progress_at=now,
                )
                for tid in missing_ids
            ],
            ignore_conflicts=True,
        )
    AISTTestMeta.objects.bulk_create(
        [AISTTestMeta(test_id=tid) for tid in test_ids],
        ignore_conflicts=True,
    )


def _sleep_poll() -> None:
    time.sleep(DEDUP_POLL_SLEEP_S)


def _process_remaining_dedup(
    *,
    pipeline: AISTPipeline,
    pipeline_id: str,
    test_ids: list[int],
    now,
    deadlines: dict,
    logger,
) -> bool:
    progress_qs = (
        TestDeduplicationProgress.objects
        .filter(test_id__in=test_ids, deduplication_complete=False)
    )
    progress_qs.filter(started_at__isnull=True).update(
        started_at=now,
        last_progress_at=now,
    )
    progress_qs.filter(last_progress_at__isnull=True).update(last_progress_at=now)

    has_max_wait_timeout = progress_qs.filter(started_at__lt=deadlines["max_wait"]).exists()
    if has_max_wait_timeout:
        logger.error(
            "Deduplication timeout reached; releasing pipeline (pipeline_id=%s).",
            pipeline_id,
        )
        _release_pipeline_after_dedup(pipeline)
        return True

    has_retries_exhausted = progress_qs.filter(
        reconcile_attempts__gte=DEDUP_RECONCILE_MAX_ATTEMPTS,
        last_progress_at__lt=deadlines["stale"],
    ).exists()
    if has_retries_exhausted:
        logger.error(
            "Deduplication retries exhausted; releasing pipeline (pipeline_id=%s).",
            pipeline_id,
        )
        _release_pipeline_after_dedup(pipeline)
        return True

    stale_ids = list(
        progress_qs
        .filter(last_progress_at__lt=deadlines["stale"])
        .exclude(last_reconcile_at__gt=deadlines["reconcile"])
        .exclude(reconcile_attempts__gte=DEDUP_RECONCILE_MAX_ATTEMPTS)
        .values_list("test_id", flat=True),
    )
    if stale_ids:
        reconcile_deduplication.delay(
            batch_size=DEDUP_RECONCILE_BATCH_SIZE,
            test_ids=stale_ids,
            stale_only=True,
        )
    return False


def _process_duplicate_cleanup(
    *,
    pipeline: AISTPipeline,
    pipeline_id: str,
    test_ids: list[int],
    deadlines: dict,
    logger,
    last_duplicate_delete_trigger_at: float | None,
) -> tuple[bool, float | None, bool]:
    has_duplicates = Finding.objects.filter(test_id__in=test_ids, duplicate=True).exists()
    if not has_duplicates:
        return False, last_duplicate_delete_trigger_at, False

    now_ts = time.time()
    should_trigger_delete = (
        last_duplicate_delete_trigger_at is None
        or (now_ts - last_duplicate_delete_trigger_at) >= DEDUP_DUPLICATE_DELETE_TRIGGER_INTERVAL_S
    )
    if should_trigger_delete:
        async_dupe_delete.delay()
        last_duplicate_delete_trigger_at = now_ts

    progress_qs = TestDeduplicationProgress.objects.filter(test_id__in=test_ids)
    has_max_wait_timeout = progress_qs.filter(started_at__lt=deadlines["max_wait"]).exists()
    if has_max_wait_timeout:
        logger.error(
            "Duplicate cleanup timeout reached; releasing pipeline (pipeline_id=%s).",
            pipeline_id,
        )
        _release_pipeline_after_dedup(pipeline)
        return True, last_duplicate_delete_trigger_at, True

    return False, last_duplicate_delete_trigger_at, True


@shared_task(bind=True)
def watch_deduplication(self, pipeline_id: str, log_level, async_user=None) -> None:
    """
    Monitor deduplication progress and finalise the pipeline.

    This task polls the ``Test`` model to determine when all imported
    tests have completed deduplication.  Once deduplication is
    finished the pipeline is marked as awaiting AI results and then
    immediately set to finished. Long running polling inside a
    Celery task is acceptable here because we perform a short sleep
    between checks and exit when complete.
    """
    pipeline = AISTPipeline.objects.get(id=pipeline_id)

    logger = install_pipeline_logging(pipeline_id, log_level)
    if not pipeline.tests.exists():
        finish_pipeline(pipeline, degraded=True)
        logger.warning("No tests to wait")
        return

    test_ids = list(pipeline.tests.values_list("id", flat=True))
    _ensure_dedup_tracking_rows(test_ids)

    # Poll until all deduplication flags are true.
    # The watcher does not "do work" itself: it only triggers reconcile when progress stalls.
    last_duplicate_delete_trigger_at: float | None = None
    try:
        while True:
            # Reload pipeline to capture any manual status change
            pipeline.refresh_from_db()
            # Exit early if user or another task finished the pipeline
            if is_terminal_pipeline_status(pipeline.status):
                return
            now = timezone.now()
            deadlines = _dedup_deadlines(now)

            # Query for tests still undergoing deduplication
            remaining = AISTTestMeta.objects.filter(
                test_id__in=test_ids,
                deduplication_complete=False,
            ).count()
            if remaining > 0:
                should_stop = _process_remaining_dedup(
                    pipeline=pipeline,
                    pipeline_id=pipeline_id,
                    test_ids=test_ids,
                    now=now,
                    deadlines=deadlines,
                    logger=logger,
                )
                if should_stop:
                    return
                _sleep_poll()
                continue

            should_stop, last_duplicate_delete_trigger_at, has_duplicates = _process_duplicate_cleanup(
                pipeline=pipeline,
                pipeline_id=pipeline_id,
                test_ids=test_ids,
                deadlines=deadlines,
                logger=logger,
                last_duplicate_delete_trigger_at=last_duplicate_delete_trigger_at,
            )
            if should_stop:
                return
            if has_duplicates:
                _sleep_poll()
                continue

            _release_pipeline_after_dedup(pipeline)
            return

    except Exception as exc:
        logger.error("Exception while waiting for deduplication to finish: %s", exc)
        finish_pipeline(pipeline, degraded=True)


@shared_task(
    name="aist.reconcile_deduplication",
    rate_limit="5/s",                    # throttle to protect the DB
    autoretry_for=(OperationalError,),   # retry on transient DB issues
    retry_backoff=True,
    retry_jitter=True,
    max_retries=5,
)
def reconcile_deduplication(
    batch_size: int = 200,
    max_runtime_s: int = 50,
    async_user=None,
    *,
    test_ids: list[int] | None = None,
    stale_only: bool = True,
) -> dict:
    """
    Reconcile deduplication progress and requeue missing findings when progress stalls.

    How it works:
    - Always refresh pending_tasks based on ProcessedFinding (single source of truth).
    - If stale_only is True, only requeue tests with no recent progress and no recent reconcile.
    - If stale_only is False, reconcile all matching tests (useful for manual/cron runs).

    Concurrency:
    - Uses row-level lock on TestDeduplicationProgress with skip_locked=True to avoid contention.
    """
    start = time.time()
    processed = 0
    requeued = 0
    now = timezone.now()
    deadlines = _dedup_deadlines(now)

    # Fetch candidate Tests; optionally restrict to specific test_ids.
    metas_qs = AISTTestMeta.objects.filter(deduplication_complete=False)
    if test_ids:
        metas_qs = metas_qs.filter(test_id__in=test_ids)
    metas = (
        metas_qs
        .select_related("test", "test__dedupe_progress")
        .order_by("test__updated" if hasattr(Test, "updated") else "test__id")[:batch_size]
    )

    for meta in metas:
        test = meta.test
        if time.time() - start > max_runtime_s:
            break

        try:
            prog = test.dedupe_progress
        except TestDeduplicationProgress.DoesNotExist:
            prog = None

        if prog is None:
            continue  # no progress row yet — a later run can create/handle it

        # Strict per-item reconciliation with row lock on the progress record.
        with transaction.atomic():
            # Lock just this progress row; if another worker already locked it,
            # skip it now to avoid contention and try others.
            locked_prog = (
                TestDeduplicationProgress.objects
                .select_for_update(skip_locked=True)
                .get(pk=prog.pk)
            )

            # Always refresh pending to sync state with DB (SSOT: ProcessedFinding).
            locked_prog.refresh_pending_tasks()
            locked_prog.refresh_from_db()

            # Nothing to do if complete or attempts exceeded.
            if locked_prog.deduplication_complete:
                processed += 1
                continue

            if locked_prog.reconcile_attempts >= DEDUP_RECONCILE_MAX_ATTEMPTS:
                processed += 1
                continue

            # When stale_only is set, requeue only if:
            # - no recent progress
            # - no recent reconcile
            if stale_only:
                if not _progress_is_stale(
                    locked_prog,
                    stale_deadline=deadlines["stale"],
                    reconcile_deadline=deadlines["reconcile"],
                ):
                    processed += 1
                    continue

            # Requeue missing findings (no ProcessedFinding) for this test.
            requeued += _requeue_missing_findings(
                test_id=locked_prog.test_id,
                batch_size=batch_size,
                logger=install_pipeline_logging("reconcile_dedup", "INFO"),
            )
            locked_prog.last_reconcile_at = now
            locked_prog.reconcile_attempts += 1
            locked_prog.save(update_fields=["last_reconcile_at", "reconcile_attempts"])
            processed += 1
    return {
        "processed": processed,
        "requeued": requeued,
        "batch_size": batch_size,
        "duration_s": round(time.time() - start, 3),
    }
