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
import json
import logging
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("aist-triage-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# ── Configuration via environment variables ──────────────────────────────────

AIST_WORKING_DIR = os.environ.get("AIST_WORKING_DIR", "/app/aist")
AIST_SERVICE_TOKEN = os.environ.get("AIST_SERVICE_TOKEN", "")
CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")
TRIAGE_TIMEOUT = int(os.environ.get("AIST_LOCAL_TRIAGE_TIMEOUT", "1800"))  # 30 min


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


def _log_claude_event(task_label: str, raw: str) -> None:
    """Parse a stream-json line from Claude Code and log a human-readable summary."""
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        logger.info("[%s] %s", task_label, raw[:300])
        return

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
                logger.info("[%s] Tool %s: %s", task_label, name, detail)
            elif btype == "text":
                text = block.get("text", "")
                if text:
                    logger.info("[%s] %s", task_label, text[:500])
            elif btype == "thinking":
                thinking = block.get("thinking", "")
                if thinking:
                    logger.info("[%s] Thinking: %s", task_label, thinking[:200])

    elif ev_type == "result":
        subtype = ev.get("subtype", "")
        duration = ev.get("duration_ms", 0)
        turns = ev.get("num_turns", 0)
        result_text = ev.get("result", "")[:300]
        logger.info("[%s] Result: %s (%ds, %d turns) %s", task_label, subtype, duration // 1000, turns, result_text)

    elif ev_type == "system":
        subtype = ev.get("subtype", "")
        if subtype == "init":
            logger.info("[%s] Session started (cwd=%s)", task_label, ev.get("cwd", "?"))
        elif subtype == "task_started":
            logger.info("[%s] Subagent started: %s", task_label, ev.get("description", ""))
        elif subtype == "task_notification":
            status = ev.get("status", "")
            logger.info("[%s] Subagent %s: %s", task_label, status, ev.get("summary", ""))

    elif ev_type == "rate_limit_event":
        info = ev.get("rate_limit_info", {})
        logger.warning("[%s] Rate limit: %s (resets %s)", task_label, info.get("status"), info.get("resetsAt"))


def _build_skill_prompt(skill_name: str, project_id: str, source_path: str, extra_args: str) -> str:
    """Read SKILL.md and build a prompt with arguments."""
    # .codex/skills/ contains the actual skill content
    # .claude/skills/ contains only redirects
    skill_path = f"{AIST_WORKING_DIR}/.codex/skills/{skill_name}/SKILL.md"

    try:
        skill_content = Path(skill_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"Error: skill {skill_name} not found at {skill_path}"

    args = f"project_id={project_id} source_path={source_path}"
    if "target_repo_path" in skill_content:
        args = f"project_id={project_id} target_repo_path={source_path}"
    if extra_args:
        args += f" {extra_args}"

    return f"Execute the following skill with these arguments: {args}\n\n{skill_content}"


async def _run_claude_skill(req: AnalyzeRequest) -> None:
    """Run claude -p in a subprocess, then optionally POST result to the callback URL."""
    prompt = _build_skill_prompt(req.skill_name, req.project_id, req.source_path, req.extra_args)

    cmd = [
        CLAUDE_PATH,
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--verbose",
        "--output-format", "stream-json",
    ]
    task_label = f"{req.skill_name} project={req.project_id}"
    logger.info("Starting claude -p for %s cwd=%s", task_label, AIST_WORKING_DIR)

    result: CallbackPayload
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=AIST_WORKING_DIR,
        )

        async def _stream_stderr(stream, collect: list[bytes]) -> None:
            async for line in stream:
                collect.append(line)
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.warning("[%s] stderr: %s", task_label, text)

        async def _stream_stdout(stream) -> None:
            async for line in stream:
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                _log_claude_event(task_label, text)

        stderr_lines: list[bytes] = []

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _stream_stdout(proc.stdout),
                    _stream_stderr(proc.stderr, stderr_lines),
                    proc.wait(),
                ),
                timeout=TRIAGE_TIMEOUT,
            )
        except TimeoutError:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except TimeoutError:
                proc.kill()
            logger.error("claude -p timed out after %ds for %s", TRIAGE_TIMEOUT, task_label)
            result = CallbackPayload(
                status="error",
                detail=f"claude -p timed out after {TRIAGE_TIMEOUT}s",
            )
        else:
            if proc.returncode == 0:
                logger.info("claude -p succeeded for %s", task_label)
                result = CallbackPayload(status="success")
            else:
                err = b"".join(stderr_lines).decode("utf-8", errors="replace")[:2000]
                logger.error("claude -p failed for %s exit=%s", task_label, proc.returncode)
                result = CallbackPayload(status="error", detail=err)
    except Exception:
        logger.exception("claude -p crashed for %s", task_label)
        result = CallbackPayload(status="error", detail="bridge internal error")

    # POST callback (optional — analyze skills persist directly, triage uses callback)
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


@app.get("/health")
async def health():
    return {"status": "ok", "running_tasks": len(_running_tasks)}
