"""
aist-triage-bridge — lightweight HTTP server that receives skill execution requests
from the celeryworker (via Unix domain socket) and runs Claude Code CLI in
the container to execute AIST skills.

Communication:
    celeryworker → UDS /run/codex-bridge/bridge.sock → this server
    this server  → claude -p (subprocess in container)
    this server  → POST callback_url (AIST API via Docker network, optional)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import re
import signal
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("aist-triage-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# ── Suppress health-check noise from uvicorn access log ──────────────────────


class _HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/health" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_HealthCheckFilter())

# ── Configuration via environment variables ──────────────────────────────────

AIST_WORKING_DIR = os.environ.get("AIST_WORKING_DIR", "/app/aist")
AIST_SERVICE_TOKEN = os.environ.get("AIST_SERVICE_TOKEN", "")
CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")
TRIAGE_TIMEOUT = int(os.environ.get("AIST_LOCAL_TRIAGE_TIMEOUT", "1800"))  # 30 min
# Grace period after a result event before SIGTERM → SIGKILL of the claude CLI.
POST_RESULT_GRACE = int(os.environ.get("AIST_LOCAL_TRIAGE_POST_RESULT_GRACE", "10"))
# Optional: directory where per-pipeline .log files are written (same path as Django MEDIA_ROOT/aist_logs).
AIST_LOG_DIR = os.environ.get("AIST_LOG_DIR", "")


# ── Models ───────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    skill_name: str
    project_id: str
    source_path: str
    callback_url: str = ""
    extra_args: str = ""


class CallbackPayload(BaseModel):
    status: str  # "success" | "error"
    detail: str = ""


# ── Background task runner ───────────────────────────────────────────────────

_running_tasks: set[asyncio.Task] = set()


def _open_pipeline_log_handler(pipeline_id: str) -> logging.handlers.RotatingFileHandler | None:
    """Return a file handler writing to the shared pipeline log, or None if AIST_LOG_DIR is unset."""
    if not AIST_LOG_DIR:
        return None
    log_path = Path(AIST_LOG_DIR) / f"{pipeline_id}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
    except OSError:
        logger.warning("Cannot open pipeline log file %s", log_path)
        return None
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        return handler


def _payload_from_result_event(ev: dict) -> CallbackPayload:
    """
    Map a stream-json ``result`` event to a bridge ``CallbackPayload``.

    Claude CLI emits ``subtype=success`` on normal completion and
    ``error_max_turns`` / ``error_during_execution`` on failure. Anything
    other than ``success`` is treated as an error and the detail is the
    first ~500 chars of the result text so it shows up in pipeline logs.
    """
    subtype = ev.get("subtype", "") or ""
    result_text = (ev.get("result", "") or "")[:500]
    if subtype == "success":
        return CallbackPayload(status="success")
    detail = f"claude -p result subtype={subtype or 'unknown'}"
    if result_text:
        detail = f"{detail}: {result_text}"
    return CallbackPayload(status="error", detail=detail)


def _log_claude_event(log: logging.Logger, task_label: str, ev: dict) -> None:
    """Log a pre-parsed stream-json event from Claude Code."""
    ev_type = ev.get("type", "")

    if ev_type == "assistant":
        msg = ev.get("message", {})
        for block in msg.get("content", []):
            btype = block.get("type", "")
            if btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                detail = ""
                if name == "Bash":
                    detail = inp.get("description") or inp.get("command", "")[:120]
                elif name == "Read":
                    detail = inp.get("file_path", "")
                elif name in {"Glob", "Grep"}:
                    detail = inp.get("pattern", "")
                elif name in {"Agent", "Skill"}:
                    detail = inp.get("description", "") or inp.get("skill", "")
                elif name in {"Edit", "Write"}:
                    detail = inp.get("file_path", "")
                else:
                    detail = str(inp)[:100]
                log.info("[%s] Tool %s: %s", task_label, name, detail)
            elif btype == "text":
                text = block.get("text", "")
                if text:
                    log.info("[%s] %s", task_label, text[:500])
            elif btype == "thinking":
                thinking = block.get("thinking", "")
                if thinking:
                    log.info("[%s] Thinking: %s", task_label, thinking[:200])

    elif ev_type == "result":
        subtype = ev.get("subtype", "")
        duration = ev.get("duration_ms", 0)
        turns = ev.get("num_turns", 0)
        result_text = ev.get("result", "")[:300]
        log.info("[%s] Result: %s (%ds, %d turns) %s", task_label, subtype, duration // 1000, turns, result_text)

    elif ev_type == "system":
        subtype = ev.get("subtype", "")
        if subtype == "init":
            log.info("[%s] Session started (cwd=%s)", task_label, ev.get("cwd", "?"))
        elif subtype == "task_started":
            log.info("[%s] Subagent started: %s", task_label, ev.get("description", ""))
        elif subtype == "task_notification":
            status = ev.get("status", "")
            log.info("[%s] Subagent %s: %s", task_label, status, ev.get("summary", ""))

    elif ev_type == "rate_limit_event":
        info = ev.get("rate_limit_info", {})
        log.warning("[%s] Rate limit: %s (resets %s)", task_label, info.get("status"), info.get("resetsAt"))


_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _build_skill_prompt(skill_name: str, project_id: str, source_path: str, extra_args: str) -> str:
    """Read SKILL.md and build a prompt with arguments."""
    # .codex/skills/ contains the actual skill content
    # .claude/skills/ contains only redirects.
    # Validate skill_name against a strict allow-list before using it as a
    # path component — otherwise a caller controlling skill_name (any
    # process with access to the bridge UDS) could traverse out of the
    # skills directory and read arbitrary files into the Claude prompt.
    if not _SKILL_NAME_RE.match(skill_name or ""):
        return f"Error: invalid skill name {skill_name!r}"

    skills_root = (Path(AIST_WORKING_DIR) / ".codex" / "skills").resolve()
    skill_path = (skills_root / skill_name / "SKILL.md").resolve()
    try:
        skill_path.relative_to(skills_root)
    except ValueError:
        return f"Error: skill path escapes skills directory: {skill_path}"

    try:
        skill_content = skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"Error: skill {skill_name} not found at {skill_path}"

    # extra_args is appended verbatim into the skill prompt. Reject newlines
    # so YAML values that carry one cannot inject prompt content. The
    # bridge already runs claude in a constrained environment, but
    # defense-in-depth on the prompt boundary is cheap.
    if extra_args and any(ch in extra_args for ch in ("\n", "\r")):
        return "Error: extra_args contains forbidden newline characters"

    args = f"project_id={project_id} source_path={source_path}"
    if "target_repo_path" in skill_content:
        args = f"project_id={project_id} target_repo_path={source_path}"
    if extra_args:
        args += f" {extra_args}"

    return f"Execute the following skill with these arguments: {args}\n\n{skill_content}"


async def _execute_claude_skill(req: AnalyzeRequest) -> CallbackPayload:
    """
    Run claude -p in a subprocess and return its CallbackPayload.

    Completion is signalled by the stream-json ``result`` event rather than by the
    OS exit code: Claude CLI sometimes lingers after emitting its final result
    (background work, subagent cleanup). Waiting for process exit made a
    successful triage look like a timeout (see pipeline d5d0aa24). We instead:

    1. Trust the first ``result`` event as the authoritative outcome.
    2. SIGTERM the process and allow ``POST_RESULT_GRACE`` seconds for a clean
       shutdown; SIGKILL if it still refuses to exit.
    3. Fall back to the ``TRIAGE_TIMEOUT`` / exit-code paths only when no
       result event is emitted at all.

    Callers decide what to do with the payload:
    - ``_run_claude_skill`` POSTs it to ``req.callback_url`` (existing async flow).
    - The ``/analyze-sync`` endpoint returns it directly to the HTTP caller.
    """
    prompt = _build_skill_prompt(req.skill_name, req.project_id, req.source_path, req.extra_args)

    cmd = [
        CLAUDE_PATH,
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--verbose",
        "--output-format", "stream-json",
    ]
    task_label = f"{req.skill_name} project={req.project_id}"

    # Per-task logger: propagates to root (uvicorn stream) AND optionally writes to pipeline log file.
    log = logging.getLogger(f"aist-triage-bridge.task.{req.project_id}")
    log.propagate = True
    file_handler = _open_pipeline_log_handler(req.project_id)
    if file_handler:
        log.addHandler(file_handler)

    log.info("Starting claude -p for %s cwd=%s", task_label, AIST_WORKING_DIR)

    result: CallbackPayload
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=AIST_WORKING_DIR,
        )

        stderr_lines: list[bytes] = []
        result_event = asyncio.Event()
        result_payload: dict[str, CallbackPayload] = {}

        async def _stream_stderr(stream) -> None:
            async for line in stream:
                stderr_lines.append(line)
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.warning("[%s] stderr: %s", task_label, text)

        async def _stream_stdout(stream) -> None:
            async for line in stream:
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                try:
                    ev = json.loads(text)
                except json.JSONDecodeError:
                    log.info("[%s] %s", task_label, text[:300])
                    continue
                _log_claude_event(log, task_label, ev)
                if ev.get("type") == "result" and not result_event.is_set():
                    result_payload["payload"] = _payload_from_result_event(ev)
                    result_event.set()

        stdout_task = asyncio.create_task(_stream_stdout(proc.stdout))
        stderr_task = asyncio.create_task(_stream_stderr(proc.stderr))
        proc_wait_task = asyncio.create_task(proc.wait())
        result_wait_task = asyncio.create_task(result_event.wait())

        done, _pending = await asyncio.wait(
            {result_wait_task, proc_wait_task},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=TRIAGE_TIMEOUT,
        )

        if result_wait_task in done:
            result = result_payload.get("payload") or CallbackPayload(
                status="error", detail="claude -p result event missing payload",
            )
            log.info(
                "claude -p result event received for %s status=%s; terminating CLI (pid=%s)",
                task_label, result.status, proc.pid,
            )
            if proc.returncode is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.shield(proc_wait_task), timeout=POST_RESULT_GRACE)
                except TimeoutError:
                    log.warning(
                        "claude -p did not exit %ds after result event for %s pid=%s; killing",
                        POST_RESULT_GRACE, task_label, proc.pid,
                    )
                    proc.kill()
                    with contextlib.suppress(asyncio.CancelledError):
                        await proc_wait_task
        elif proc_wait_task in done:
            rc = proc.returncode
            if rc == 0:
                log.info("claude -p succeeded for %s (no result event, exit=0)", task_label)
                result = CallbackPayload(status="success")
            else:
                err = b"".join(stderr_lines).decode("utf-8", errors="replace")[:2000]
                log.error("claude -p failed for %s exit=%s", task_label, rc)
                result = CallbackPayload(status="error", detail=err or f"claude -p exit={rc}")
        else:
            last_stderr = b"".join(stderr_lines[-20:]).decode("utf-8", errors="replace")[:1000]
            log.error(
                "claude -p timed out after %ds for %s pid=%s returncode=%s; last stderr: %s",
                TRIAGE_TIMEOUT, task_label, proc.pid, proc.returncode, last_stderr or "<empty>",
            )
            if proc.returncode is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.shield(proc_wait_task), timeout=POST_RESULT_GRACE)
                except TimeoutError:
                    proc.kill()
                    with contextlib.suppress(asyncio.CancelledError):
                        await proc_wait_task
            result = CallbackPayload(
                status="error",
                detail=f"claude -p timed out after {TRIAGE_TIMEOUT}s",
            )

        # Drain streaming tasks so we don't leak them; short deadline is enough
        # because the process is either gone or about to be killed.
        for drain_task in (stdout_task, stderr_task):
            if drain_task.done():
                continue
            try:
                await asyncio.wait_for(asyncio.shield(drain_task), timeout=5)
            except TimeoutError:
                drain_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain_task
        if not result_wait_task.done():
            result_wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await result_wait_task
    except Exception:
        log.exception("claude -p crashed for %s", task_label)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        result = CallbackPayload(status="error", detail="bridge internal error")
    finally:
        if file_handler:
            log.removeHandler(file_handler)
            file_handler.close()

    return result


async def _run_claude_skill(req: AnalyzeRequest) -> None:
    """
    Execute the skill and POST the result to ``req.callback_url`` (if set).

    Used by the async ``/analyze`` endpoint where the caller registers a
    callback URL and continues without waiting. ``/analyze-sync`` callers
    use ``_execute_claude_skill`` directly instead.
    """
    task_label = f"{req.skill_name} project={req.project_id}"
    result = await _execute_claude_skill(req)

    if req.callback_url:
        headers = {"Content-Type": "application/json"}
        if AIST_SERVICE_TOKEN:
            headers["Authorization"] = f"Token {AIST_SERVICE_TOKEN}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(req.callback_url, json=result.model_dump(), headers=headers)
                resp.raise_for_status()
            logger.info("Callback sent for %s status=%s", task_label, result.status)
        except Exception:
            logger.exception("Failed to send callback for %s", task_label)
    else:
        logger.info("No callback URL for %s; result=%s", task_label, result.status)


def _schedule_task(req: AnalyzeRequest) -> None:
    """Schedule the claude skill coroutine and track it."""
    task = asyncio.get_event_loop().create_task(_run_claude_skill(req))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)


# ── FastAPI app ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    for task in list(_running_tasks):
        task.cancel()
    if _running_tasks:
        await asyncio.gather(*_running_tasks, return_exceptions=True)


app = FastAPI(title="aist-triage-bridge", lifespan=lifespan)


@app.post("/analyze", status_code=202)
async def analyze(req: AnalyzeRequest):
    if not req.skill_name or not req.source_path:
        raise HTTPException(status_code=400, detail="skill_name and source_path are required")
    _schedule_task(req)
    return {"accepted": True, "skill_name": req.skill_name, "project_id": req.project_id}


@app.post("/analyze-sync")
async def analyze_sync(req: AnalyzeRequest):
    """
    Run the skill and block until it produces a result.

    Used by the SAST pipeline's analyzer_runner when it needs the skill's
    output file on disk before continuing the pipeline (e.g.
    claude-diff-security writing its Generic Findings Import JSON).
    """
    if not req.skill_name or not req.source_path:
        raise HTTPException(status_code=400, detail="skill_name and source_path are required")
    result = await _execute_claude_skill(req)
    return result.model_dump()


@app.get("/health")
async def health():
    return {"status": "ok", "running_tasks": len(_running_tasks)}
