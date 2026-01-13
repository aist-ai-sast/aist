from __future__ import annotations

import json
import time

from django.db import close_old_connections
from django.db.models import Count
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from dojo.aist.logging_transport import get_redis
from dojo.aist.models import AISTPipeline, AISTStatus, TestDeduplicationProgress
from dojo.aist.views._common import ERR_PIPELINE_NOT_FOUND


def pipeline_status_stream(request, pipeline_id: str):
    """SSE endpoint: sends 'status' on status change; finishes with 'done' on FINISHED/FAILED/DELETED."""
    # Quick existence check
    if not AISTPipeline.objects.filter(id=pipeline_id).exists():
        raise Http404(ERR_PIPELINE_NOT_FOUND)

    def event_stream():
        last_status = None
        heartbeat_every = 3  # seconds
        last_heartbeat = 0.0

        try:
            while True:
                # Important for long-lived streams
                close_old_connections()

                # Re-fetch object every loop, don't keep a live instance
                obj = (
                    AISTPipeline.objects
                    .only("id", "status", "updated")
                    .filter(id=pipeline_id)
                    .first()
                )

                if obj is None:
                    # Deleted — inform client and exit
                    yield "event: done\ndata: deleted\n\n"
                    break

                if obj.status != last_status:
                    last_status = obj.status
                    # Proper SSE block: event, data, blank line
                    yield f"event: status\ndata: {last_status}\n\n"

                    if last_status in {
                        getattr(AISTStatus, "FINISHED", "FINISHED"),
                        getattr(AISTStatus, "FAILED", "FAILED"),
                    }:  # PLR6201
                        yield "event: done\ndata: finished\n\n"
                        break

                # Heartbeat so proxies (e.g., Nginx) don't close the connection
                now_ts = time.time()
                if now_ts - last_heartbeat >= heartbeat_every:
                    last_heartbeat = now_ts
                    yield f": heartbeat {int(now_ts)}\n\n"

                time.sleep(1)

        except GeneratorExit:
            # Client closed connection — just exit
            return
        finally:
            close_old_connections()

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # important for Nginx to avoid buffering
    return resp


def deduplication_progress_json(request, pipeline_id: str):
    """Return deduplication progress for a pipeline as JSON (per Test and overall). Progress counts findings, not just tests with a boolean flag."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)

    tests = (
        pipeline.tests
        .select_related("engagement")
        .annotate(total_findings=Count("finding", distinct=True))
        .order_by("id")
    )

    tests_payload = []
    overall_total = 0
    overall_processed = 0

    for t in tests:
        # Ensure progress row exists and is refreshed if needed
        prog, _ = TestDeduplicationProgress.objects.get_or_create(test=t)
        # `pending_tasks` = findings total - processed; we keep `refresh_pending_tasks()` the SSOT.
        prog.refresh_pending_tasks()

        total = getattr(t, "total_findings", 0)
        pending = prog.pending_tasks
        processed = max(total - pending, 0)
        pct = 100 if total == 0 else int(processed * 100 / total)

        overall_total += total
        overall_processed += processed

        tests_payload.append({
            "test_id": t.id,
            "test_name": getattr(t, "title", None) or f"Test #{t.id}",
            "total_findings": total,
            "processed": processed,
            "pending": pending,
            "percent": pct,
            "completed": bool(prog.deduplication_complete),
        },
        )

    overall_pct = 100 if overall_total == 0 else int(overall_processed * 100 / overall_total)

    return JsonResponse({
        "status": pipeline.status,
        "overall": {
            "total_findings": overall_total,
            "processed": overall_processed,
            "pending": max(overall_total - overall_processed, 0),
            "percent": overall_pct,
        },
        "tests": tests_payload,
    },
    )


@csrf_exempt
@require_http_methods(["GET"])
def pipeline_enrich_progress_sse(request, pipeline_id: str):
    redis = get_redis()
    key = f"aist:progress:{pipeline_id}:enrich"

    def event_stream():
        last = None
        last_ping = time.monotonic()
        while True:
            try:
                total, done = redis.hmget(key, "total", "done")
            except Exception:
                total, done = 0, 0
            total = int(total or 0)
            done = int(done or 0)

            payload = {
                "total": total,
                "done": done,
                "percent": (100 if total == 0 else int(done * 100 / total)),
            }

            now = (payload["total"], payload["done"])
            if now != last:
                yield f"data: {json.dumps(payload)}\n\n"
                last = now

            # heartbeat so proxy doesn't close connection
            if time.monotonic() - last_ping > 25:
                yield ": ping\n\n"
                last_ping = time.monotonic()

            if total and done >= total:
                yield "event: done\ndata: ok\n\n"
                break

            time.sleep(1)

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp
