from __future__ import annotations

import json
import pathlib
import time
from contextlib import suppress

from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from dojo.aist.logging_transport import BACKLOG_COUNT, PUBSUB_CHANNEL_TPL, STREAM_KEY, get_pipeline_log_path, get_redis
from dojo.aist.models import AISTPipeline, AISTStatus
from dojo.aist.views._common import ERR_PIPELINE_NOT_FOUND


@require_http_methods(["GET"])
def pipeline_logs_progressive(request, pipeline_id: str):
    """
    Progressive log API similar to Jenkins/GitLab.
    GET params:
    - start=<int>: byte offset to read from (default 0). Returns data from this offset to EOF.
    - tail=<int>: last N lines to return initially (ignored if start is provided).
    Response headers:
    - X-Log-Size: current file size in bytes (use as next start).
    Body:
    - plain text chunk (UTF-8).
    """
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    path = get_pipeline_log_path(pipeline.id)
    data = ""
    size = 0
    start = request.GET.get("start")
    tail = request.GET.get("tail")

    # parse params
    try:
        start = int(start) if start is not None else None
    except ValueError:
        start = None
    try:
        tail = max(0, int(tail)) if tail is not None else None
    except ValueError:
        tail = None

    if pathlib.Path(path).exists():
        size = pathlib.Path(path).stat().st_size
        # if tail return latest N lines
        if tail:
            # read last N lines
            with pathlib.Path(path).open("rb") as f:
                lines = f.readlines()[-tail:]
            decoded = [ln.decode("utf-8", errors="ignore").rstrip("\r\n") for ln in lines]
            data = "\n".join(decoded)
        elif start is not None:
            # read from bite offset until end
            start = max(0, min(start, size))
            with pathlib.Path(path).open("rb") as f:
                f.seek(start)
                chunk = f.read()
            data = chunk.decode("utf-8", errors="ignore")
        else:
            # by default return all file
            data = pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")

    resp = HttpResponse(data, content_type="text/plain; charset=utf-8")
    resp["X-Log-Size"] = str(size)
    return resp


def get_logs_content(pipeline: AISTPipeline):
    path = get_pipeline_log_path(pipeline.id)
    return pathlib.Path(path).read_text(encoding="utf-8", errors="ignore") if pathlib.Path(path).exists() else ""


@require_http_methods(["GET"])
def pipeline_logs_full(request, pipeline_id: str) -> HttpResponse:
    """Return the entire log file as text/plain."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    content = get_logs_content(pipeline)
    return HttpResponse(content, content_type="text/plain; charset=utf-8")


@require_http_methods(["GET"])
def pipeline_logs_raw(request, pipeline_id: str) -> HttpResponse:
    """Raw log content (same as full) used by 'Copy to clipboard'."""
    return pipeline_logs_full(request, pipeline_id)


@require_http_methods(["GET"])
def pipeline_logs_download(request, pipeline_id: str) -> HttpResponse:
    """Force download of the entire log as a .log file."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    content = get_logs_content(pipeline)
    resp = HttpResponse(content, content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="pipeline-{pipeline_id}.log"'
    return resp


@csrf_exempt  # SSE does not need CSRF for GET; keep it simple
@require_http_methods(["GET"])
def stream_logs_sse(request, pipeline_id: str):
    """Server-Sent Events endpoint that streams new log lines for a pipeline. Reads from DB; emits only new tail bytes every poll tick."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)

    def event_stream():
        last_len = 0
        # Simple polling loop. Replace with channels/redis pub-sub if desired.
        for _ in range(60 * 60 * 12):  # up to ~12h
            p = AISTPipeline.objects.filter(id=pipeline.id).only("logs", "status").first()
            if not p:
                break
            data = p.logs or ""
            if len(data) > last_len:
                chunk = data[last_len:]
                last_len = len(data)
                # SSE frame
                yield f"data: {chunk}\n\n"
            if p.status == AISTStatus.FINISHED:
                yield "event: done\ndata: FINISHED\n\n"
                break
            time.sleep(0.3)

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream; charset=utf-8")
    resp["Cache-Control"] = "no-cache, no-transform"
    resp["X-Accel-Buffering"] = "no"
    return resp


def _sse_data(payload: str) -> bytes:
    """Format a single SSE 'message' event."""
    return f"data: {payload}\n\n".encode()


def _sse_comment(comment: str) -> bytes:
    """Format an SSE comment line (useful as heartbeat)."""
    return f": {comment}\n\n".encode()


def _stream_last_lines_from_redis_stream(r, pipeline_id: str, limit: int):
    """
    Send initial backlog from Redis Stream (last `limit` items) filtered by pipeline_id.
    Uses XREVRANGE for 'tail'-like behavior then reverses to chronological order.
    """
    try:
        # XREVRANGE stream + - COUNT N  -> newest first
        entries = r.xrevrange(STREAM_KEY, max="+", min="-", count=limit) or []
        # reverse to oldest -> newest for nicer UI
        for _entry_id, fields in reversed(entries):  # B007
            pid = (fields or {}).get("pipeline_id")
            msg = (fields or {}).get("message")
            lvl = (fields or {}).get("level")
            if not pid or pid != pipeline_id or not msg:
                continue
            line = f"{lvl} {msg}" if lvl else msg
            yield _sse_data(line)
    except Exception:
        # Do not break SSE if Redis is temporarily unavailable
        return


@csrf_exempt
@require_http_methods(["GET"])
def stream_logs_sse_redis_based(request: HttpRequest, pipeline_id: str) -> HttpResponse:
    """
    SSE endpoint for pipeline logs.
    1) Replays last N lines from Redis Stream for quick backlog.
    2) Subscribes to Redis Pub/Sub and streams new lines immediately.
    """
    # Validate pipeline
    try:
        AISTPipeline.objects.only("id").get(id=pipeline_id)
    except AISTPipeline.DoesNotExist:
        raise Http404(ERR_PIPELINE_NOT_FOUND)

    r = get_redis()
    channel = PUBSUB_CHANNEL_TPL.format(pipeline_id=pipeline_id)

    def event_stream():
        # 1) initial backlog from Redis Stream
        yield from _stream_last_lines_from_redis_stream(r, pipeline_id, BACKLOG_COUNT)

        # 2) subscribe for live updates
        pubsub = r.pubsub()
        pubsub.subscribe(channel)

        last_ping = time.monotonic()
        try:
            # Notify client that SSE is alive
            yield _sse_comment("connected")

            for msg in pubsub.listen():
                now = time.monotonic()
                # heartbeat every ~25s
                if now - last_ping > 25:
                    yield _sse_comment("ping")
                    last_ping = now

                if msg.get("type") != "message":
                    continue

                try:
                    data = json.loads(msg["data"])
                    txt = f'{data.get("level") or ""} {data.get("message") or ""}'.strip()
                    if txt:
                        yield _sse_data(txt)
                except Exception:
                    # If payload not JSON, try raw data
                    raw = msg.get("data")
                    if isinstance(raw, str) and raw:
                        yield _sse_data(raw)
        finally:
            with suppress(Exception):  # S110
                pubsub.unsubscribe(channel)
                pubsub.close()

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    # Avoid buffering by proxies/servers
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # for nginx
    return resp
