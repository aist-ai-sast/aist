"""
aist-triage-bridge — lightweight HTTP server that receives skill execution requests
from the celeryworker (via Unix domain socket) and runs Claude Code CLI in
the container to execute AIST skills.

Communication:
    celeryworker → UDS /run/claude-bridge/bridge.sock → this server
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
import shutil
import signal
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, SecretStr

if TYPE_CHECKING:
    from collections.abc import Callable

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
# Optional bridge-wide default model passed to ``claude -p --model``. Empty
# means "let the CLI pick its own default". A per-request ``AnalyzeRequest.model``
# overrides this. Useful to pin a model for the whole bridge container without a
# per-skill change (e.g. CLAUDE_BRIDGE_MODEL=opus).
CLAUDE_BRIDGE_MODEL = os.environ.get("CLAUDE_BRIDGE_MODEL", "")
TRIAGE_TIMEOUT = int(os.environ.get("AIST_LOCAL_TRIAGE_TIMEOUT", "10800"))  # 3 hours
# Grace period after a result event before SIGTERM → SIGKILL of the claude CLI.
POST_RESULT_GRACE = int(os.environ.get("AIST_LOCAL_TRIAGE_POST_RESULT_GRACE", "10"))
# Optional: directory where per-pipeline .log files are written (same path as Django MEDIA_ROOT/aist_logs).
AIST_LOG_DIR = os.environ.get("AIST_LOG_DIR", "")

# Mirror of ``aist.logging_transport.LOG_ROTATION_BACKUP_COUNT`` — the bridge
# runs in a separate container and cannot import from the Django app. The
# Full Log / Download endpoints in ``aist/api/pipelines.py`` enumerate this
# many numbered backups; keep both values in lockstep.
LOG_ROTATION_BACKUP_COUNT = 5
LOG_ROTATION_MAX_BYTES = 10 * 1024 * 1024

# Per-run CLAUDE_CONFIG_DIR root — session jsonl files land here so we can tail them.
# CLAUDE_CONFIG_DIR is the documented env var that redirects ~/.claude to a custom path.
_CLAUDE_RUNS_DIR = Path("/tmp/claude-bridge-runs")  # noqa: S108  -- runs inside the bridge container
_RESULT_POLL_INTERVAL = 5.0   # seconds between result-file existence checks
_JSONL_POLL_INTERVAL = 5.0    # seconds between jsonl tail reads
_JSONL_FIND_MAX_WAIT = 60     # seconds to wait for jsonl file to appear after session init

# StreamReader line-buffer limit for the claude subprocess stdout/stderr.
# ``claude --output-format stream-json`` emits one JSON object per line, and a
# single event can be huge: tool_result echoes carry the full tool output (all
# findings, large diffs), routinely exceeding asyncio's default 64 KiB line
# limit. When a line overruns that default, ``StreamReader.readline`` raises
# (LimitOverrunError → ValueError) and ``_stream_stdout`` aborts mid-stream —
# which used to be misread as a clean EOF and a false ``success`` (incident
# pipeline f4dbe4aa). Raise the ceiling so legitimate long lines stream
# intact; 64 MiB comfortably covers the largest observed tool_result echoes.
_STREAM_READER_LIMIT = 64 * 1024 * 1024

# ── Claude stream-json / jsonl schema anchors ────────────────────────────────
#
# Every literal string the bridge looks up in a Claude event lives here.
# If Anthropic renames a field or subtype in a future Claude Code release,
# fix the constant once — none of the readers below reference the raw
# strings directly. Layout is also a schema anchor: claude writes the
# session jsonl under ``CLAUDE_CONFIG_DIR/projects/<derived-cwd>/<sid>.jsonl``
# (internal layout, not officially documented; ``_JSONL_PROJECTS_DIR``
# captures the leading segment so a single edit covers a future move).

# Top-level fields on every stream-json / jsonl envelope.
_F_TYPE = "type"
_F_SUBTYPE = "subtype"
_F_SESSION_ID = "session_id"
_F_MESSAGE = "message"
_F_CONTENT = "content"
_F_RESULT = "result"

# Event ``type`` values.
_T_SYSTEM = "system"
_T_RESULT = "result"
_T_ASSISTANT = "assistant"
_T_USER = "user"
_T_RATE_LIMIT = "rate_limit_event"

# ``system`` event subtypes claude emits on stdout.
_SYS_INIT = "init"
_SYS_API_RETRY = "api_retry"
_SYS_COMPACT_BOUNDARY = "compact_boundary"
_SYS_TASK_STARTED = "task_started"
_SYS_TASK_NOTIFICATION = "task_notification"

# ``result`` subtype mapping. Use sets (not single literals) so future
# Anthropic additions can be onboarded with a one-line change instead of
# branch surgery in ``_payload_from_result_event``. Anything not in
# either set is treated as error with the subtype name surfaced for
# operator triage.
_RESULT_SUCCESS = "success"
_RESULT_SUCCESS_SUBTYPES = frozenset({_RESULT_SUCCESS})
_RESULT_ERROR_MAX_TURNS = "error_max_turns"
_RESULT_ERROR_DURING_EXECUTION = "error_during_execution"
_RESULT_KNOWN_ERROR_SUBTYPES = frozenset({
    _RESULT_ERROR_MAX_TURNS,
    _RESULT_ERROR_DURING_EXECUTION,
})

# Heuristic for Anthropic-issued credential values. Used as
# defence-in-depth when populating the ``_RedactingFilter``: even if a
# token snuck into the bridge container env (against the policy in
# Task 9 of docs/plans/2026-05-12-claude-as-org-integration.md) and
# claude echoes it on stderr, we still mask it from logs.
_TOKEN_SHAPED = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$")

# Inner message-content block ``type`` discriminators (the value of the
# ``type`` key inside a content block).
_BLOCK_TYPE_TOOL_USE = "tool_use"
_BLOCK_TYPE_TOOL_RESULT = "tool_result"
_BLOCK_TYPE_TEXT = "text"
_BLOCK_TYPE_THINKING = "thinking"

# Field names where the actual content of text/thinking blocks lives. Today
# these collide with the type-discriminator strings above (a ``"text"``
# block stores its content under ``"text"``), but they're distinct schema
# concepts — keeping them as separate constants means a future Anthropic
# split (e.g. ``{"type": "text", "value": "..."}``) is a one-line change.
_FIELD_TEXT = "text"
_FIELD_THINKING = "thinking"

# Truncation budgets for log lines emitted by ``_log_claude_event``. Three
# tiers cover the spectrum from a one-line summary (Result text in the
# CallbackPayload) up to verbose tool stdout. Keep these consistent so a
# bridge log line never has surprising width.
_LOG_TRUNC_SHORT = 500    # CallbackPayload detail field
_LOG_TRUNC_MED = 1000     # thinking blocks, end-of-run result text, last stderr
_LOG_TRUNC_LONG = 2000    # tool commands, assistant text, tool result content

# Filesystem layout under CLAUDE_CONFIG_DIR (claude internal structure):
#   <projects>/<derived-cwd>/<session_id>.jsonl              -- parent session
#   <projects>/<derived-cwd>/<session_id>/<subagents>/agent-*.jsonl
#                                                            -- one per Task subagent
# For skills that delegate to sub-agents (aist-full-security-review,
# aist-diff-security-review, ...) almost all the real Read/Bash/thinking
# happens inside the agent-*.jsonl files, so the tailer must watch them
# in addition to the parent jsonl. Sub-agent files are created lazily,
# so the watcher rescans on every tick rather than once at startup.
_JSONL_PROJECTS_DIR = "projects"
_JSONL_SUBAGENTS_DIR = "subagents"

# Event types that exist only in the stream-json stdout channel and are
# NOT mirrored to the session jsonl. ``_stream_stdout`` logs these so the
# operator still sees the session header, API-retry warnings and the
# final result summary; everything else (assistant/user content) flows
# from ``_tail_jsonl`` to keep one authoritative timeline in the bridge log.
_STDOUT_ONLY_LOG_TYPES = frozenset({_T_SYSTEM, _T_RESULT, _T_RATE_LIMIT})


# ── Models ───────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    # ``skill_name`` is used as a path component when reading SKILL.md.
    # ``_build_skill_prompt`` has its own defensive check too, but
    # validating here returns 422 immediately rather than 202-then-fail.
    skill_name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    # ``project_id`` is used as a path component for ``claude_config_dir``
    # AND for the per-pipeline bridge log filename. Without strict
    # validation, a caller could traverse out of /tmp/claude-bridge-runs
    # (``shutil.rmtree`` in ``_execute_claude_skill``'s finally) and out
    # of ``AIST_LOG_DIR``. Limit to a generous alphabet covering pipeline
    # UUIDs and integer project ids.
    project_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    source_path: str
    callback_url: str = ""
    extra_args: str = ""
    # Optional Claude model to run this skill with, passed straight through to
    # ``claude -p --model <model>``. Accepts a CLI alias (``opus``/``sonnet``/
    # ``haiku``/``fable``), an extended-context variant (``opus[1m]``), or a
    # full model id (``claude-opus-4-8``, ``claude-opus-4-8[1m]``). Empty string
    # means "no ``--model`` flag" → the CLI default (or ``CLAUDE_BRIDGE_MODEL``)
    # wins. The pattern forbids a leading ``-`` so the value can never be
    # mistaken for another CLI flag when spliced into the argv list; ``[``/``]``
    # are allowed for the documented 1M-context aliases and are safe because the
    # CLI is spawned via ``create_subprocess_exec`` (argv list, no shell).
    model: str = Field(default="", pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}$")
    # Generic per-run environment overlay injected into the claude
    # subprocess. Values are ``SecretStr`` so they mask in repr/dump/log
    # by default. The bridge is agent-agnostic at this layer: the caller
    # decides which env vars to inject (Claude OAuth token, future
    # agents' credentials, etc.). Bridge container env MUST NOT contain
    # these credentials (Task 9 enforces).
    subprocess_env: dict[str, SecretStr] = Field(default_factory=dict)


class CallbackPayload(BaseModel):
    status: str  # "success" | "error"
    detail: str = ""


# ── Background task runner ───────────────────────────────────────────────────

_running_tasks: set[asyncio.Task] = set()


def _task_label(req: AnalyzeRequest) -> str:
    """Compose the operator-facing label used in every bridge log line."""
    return f"{req.skill_name} project={req.project_id}"


@dataclass
class _RunContext:

    """
    Shared state across the coroutines that make up one ``claude -p`` run.

    Replaces what used to be a fistful of free variables captured in
    nested closures inside ``_execute_claude_skill`` (``result_event``,
    ``session_id_holder``, ``stderr_lines``, ...). Lifting them into a
    dataclass lets ``_stream_stdout`` / ``_tail_jsonl`` etc. live at
    module level, where they can be tested in isolation.
    """

    log: logging.Logger
    task_label: str
    claude_config_dir: Path
    result_file_path: Path | None
    stderr_lines: list[bytes] = field(default_factory=list)
    result_event: asyncio.Event = field(default_factory=asyncio.Event)
    payload: CallbackPayload | None = None
    session_id_known: asyncio.Event = field(default_factory=asyncio.Event)
    session_id: str = ""
    # Fires when ``_stream_stdout``'s async-for loop ends — i.e. the stdout
    # pipe write-end has been closed by ALL processes that inherited it
    # (parent claude + every subagent spawned via the Task tool). This is
    # the strongest "claude is fully done" signal we have, because subagents
    # inherit the parent's fd1 and the pipe only EOFs once every one of
    # them has exited. Used as a defence-in-depth fallback in
    # ``_execute_claude_skill`` for the asyncio ThreadedChildWatcher ↔
    # tini race that wedges ``proc.wait()`` (incident pipeline 4a1912d7).
    stdout_done: asyncio.Event = field(default_factory=asyncio.Event)
    # Set on the first non-empty assistant turn observed (from either the
    # stdout stream or the session jsonl tail — see ``_stream_stdout`` and
    # ``_process_jsonl_chunk``). The EOF-fallback uses this to distinguish
    # "claude finished its work and exited" from "claude crashed before
    # producing any output".
    had_assistant_turn: bool = False
    # Set when ``_stream_stdout``'s read loop dies with an UNEXPECTED exception.
    # Oversized stream-json lines are recovered in-stream (see
    # ``stdout_lines_dropped``); this flag covers everything else. ``stdout_done``
    # is ALSO set on such a crash (its ``finally``), so without this flag the
    # ``_execute_claude_skill`` EOF fallback cannot tell a genuine pipe EOF
    # ("claude tree fully done") from a reader crash on a still-running claude
    # — and used to report a false ``success`` (incident pipeline f4dbe4aa).
    stdout_reader_failed: bool = False
    # Count of stdout lines dropped because a single stream-json line overran
    # the StreamReader buffer even at ``_STREAM_READER_LIMIT``. Dropping such a
    # line is non-fatal: the reader keeps going so the small ``result`` event
    # that follows a giant tool_result echo is still captured. Operator-facing
    # observability only — does not change completion semantics.
    stdout_lines_dropped: int = 0


class _RedactingFilter(logging.Filter):

    """
    Mask known secret values in any log message routed through this filter.

    Installed once per ``_execute_claude_skill`` invocation onto the
    per-task logger; receives the raw secret values extracted from
    ``req.subprocess_env``. The filter operates on the **formatted**
    message (post-args substitution) so that secrets passed as %s
    arguments don't slip through.

    Agent-agnostic: holds only the specific values from the current run,
    no hardcoded prefixes / patterns. Mirrors
    ``aist/integrations/claude.py::redact_claude_secret`` on the Django
    side — the bridge cannot import from aist, hence the duplicated
    logic.
    """

    _REDACTED = "***REDACTED***"

    def __init__(self, secret_values):
        super().__init__()
        # Filter out empties so ``str.replace("", "X")`` does not insert
        # the redaction marker between every character of every message.
        self._secrets = [s for s in secret_values if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        original = msg
        for secret in self._secrets:
            msg = msg.replace(secret, self._REDACTED)
        if msg != original:
            record.msg = msg
            record.args = ()
        return True


def _open_pipeline_log_handler(
    pipeline_id: str,
    log: logging.Logger | None = None,
) -> logging.handlers.RotatingFileHandler | None:
    """
    Return a file handler writing to ``<pipeline_id>.bridge.log``.

    The bridge container runs as the unprivileged ``claude`` user (see
    ``entrypoint.sh: gosu claude``), while ``celeryworker`` writes the
    primary pipeline log file (``<pipeline_id>.log``) as root. On Linux
    these UIDs are real, so an attempt to append to a root-owned file
    fails with PermissionError and the bridge silently loses its logs.

    Writing to a separate ``<pipeline_id>.bridge.log`` keeps the bridge
    as the sole writer to its own file. The Django log API merges both
    files when the operator opens "Full Logs" / "Download" and exposes a
    second progressive endpoint so the UI can show bridge events live.

    When ``log`` is supplied and already has a handler pointing at the
    same path (concurrent runs sharing a ``pipeline_id``), returns
    ``None`` so the caller skips both attaching a second writer and
    removing it in its own ``finally`` — only the run that created the
    handler is responsible for closing it.
    """
    if not AIST_LOG_DIR:
        return None
    log_path = Path(AIST_LOG_DIR) / f"{pipeline_id}.bridge.log"
    if log is not None:
        for existing in log.handlers:
            if (
                isinstance(existing, logging.handlers.RotatingFileHandler)
                and Path(existing.baseFilename) == log_path
            ):
                return None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=LOG_ROTATION_MAX_BYTES,
            backupCount=LOG_ROTATION_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Cannot open pipeline log file %s", log_path)
        return None
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        return handler


def _signal_pgroup(pid: int, sig: int) -> None:
    """
    Send ``sig`` to the entire process group led by ``pid``.

    Claude is spawned with ``start_new_session=True`` so the CLI and every
    descendant it forks (Bash, sub-agents, ...) share one process group.
    Killing only ``pid`` leaves grandchildren alive holding the
    stdout/stderr pipes open — the bridge then never observes EOF, the
    drain coroutines never finish, and ``_execute_claude_skill`` hangs
    indefinitely (see incident with pipeline a2a7ed26 where
    aist-full-security-review sub-agents survived a SIGKILL on the CLI).
    """
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return


async def _terminate_subprocess(
    proc: asyncio.subprocess.Process,
    proc_wait_task: asyncio.Task,
    log: logging.Logger,
    task_label: str,
) -> None:
    """
    SIGTERM → grace → SIGKILL on the claude CLI's process group.

    Always targets the whole pgroup, never just ``proc.pid``, otherwise
    descendants outlive the CLI and keep the stdout/stderr pipes open.
    Both waits are time-bounded so a stuck child watcher cannot hold the
    /analyze-sync request open forever.
    """
    if proc.returncode is not None:
        return
    _signal_pgroup(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(proc_wait_task), timeout=POST_RESULT_GRACE)
    except TimeoutError:
        log.warning(
            "claude -p did not exit %ds after SIGTERM for %s pid=%s; "
            "SIGKILL on process group",
            POST_RESULT_GRACE, task_label, proc.pid,
        )
    else:
        return
    _signal_pgroup(proc.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(asyncio.shield(proc_wait_task), timeout=POST_RESULT_GRACE)
    except TimeoutError:
        log.error(
            "claude -p still alive %ds after SIGKILL on pgroup for %s pid=%s; "
            "abandoning subprocess",
            POST_RESULT_GRACE, task_label, proc.pid,
        )


def _payload_from_result_event(ev: dict) -> CallbackPayload:
    """
    Map a stream-json ``result`` event to a bridge ``CallbackPayload``.

    Claude CLI today emits ``subtype=success`` on normal completion and
    ``error_max_turns`` / ``error_during_execution`` on failure. Both
    sets are configurable above (``_RESULT_SUCCESS_SUBTYPES`` /
    ``_RESULT_KNOWN_ERROR_SUBTYPES``) so a future Anthropic addition is
    a one-line update instead of branch surgery. Unknown subtypes are
    treated as errors but logged distinctively so the operator can spot
    a drifted CLI version quickly.
    """
    subtype = ev.get(_F_SUBTYPE, "") or ""
    if subtype in _RESULT_SUCCESS_SUBTYPES:
        return CallbackPayload(status="success")
    result_text = (ev.get(_F_RESULT, "") or "")[:_LOG_TRUNC_SHORT]
    if subtype in _RESULT_KNOWN_ERROR_SUBTYPES:
        detail = f"claude -p {subtype}"
    else:
        detail = f"claude -p result subtype={subtype or 'unknown'}"
    if result_text:
        detail = f"{detail}: {result_text}"
    return CallbackPayload(status="error", detail=detail)


def _scan_env_for_anthropic_secrets() -> list[str]:
    """
    Return any ``os.environ`` values that look like Anthropic credentials.

    Used to seed the per-run ``_RedactingFilter`` with values the caller
    didn't pass through ``req.subprocess_env``. Task 9 of the
    Claude-as-Integration refactor mandates the bridge container env
    NOT contain agent credentials — this scan is the runtime guard
    against an operator who forgot.
    """
    return [v for v in os.environ.values() if isinstance(v, str) and _TOKEN_SHAPED.match(v)]


def _format_tool_use_detail(name: str, inp: dict) -> str:
    """
    Compose the human-readable detail string for a ``tool_use`` block.

    Centralizes the per-tool extraction rules so ``_log_assistant_event``
    stays a single line per block. Each branch picks the field that's
    most informative for the operator (a Bash command, a file path, a
    glob pattern, ...) and falls back to the raw input dict otherwise.
    """
    if name == "Bash":
        return inp.get("description") or inp.get("command", "")[:_LOG_TRUNC_LONG]
    if name == "Read":
        return inp.get("file_path", "")
    if name in {"Glob", "Grep"}:
        return inp.get("pattern", "")
    if name in {"Agent", "Skill", "Task"}:
        return inp.get("description", "") or inp.get("skill", "")
    if name in {"Edit", "Write"}:
        return inp.get("file_path", "")
    return str(inp)[:_LOG_TRUNC_SHORT]


def _log_assistant_event(log: logging.Logger, task_label: str, ev: dict) -> None:
    msg = ev.get(_F_MESSAGE) or {}
    content = msg.get(_F_CONTENT)
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get(_F_TYPE, "")
        if btype == _BLOCK_TYPE_TOOL_USE:
            name = block.get("name", "?")
            detail = _format_tool_use_detail(name, block.get("input", {}))
            log.info("[%s] Tool %s: %s", task_label, name, detail)
        elif btype == _BLOCK_TYPE_TEXT:
            text = block.get(_FIELD_TEXT, "")
            if text:
                log.info("[%s] %s", task_label, text[:_LOG_TRUNC_LONG])
        elif btype == _BLOCK_TYPE_THINKING:
            thinking = block.get(_FIELD_THINKING, "")
            if thinking:
                log.info("[%s] Thinking: %s", task_label, thinking[:_LOG_TRUNC_MED])


def _log_user_event(log: logging.Logger, task_label: str, ev: dict) -> None:
    # Tool results — output of every tool execution (bash stdout, file
    # contents, etc.). The first user turn in the JSONL session is the
    # initial prompt as a raw string; that's logged via stream-json
    # ``system/init`` instead, so we silently skip string content here.
    msg = ev.get(_F_MESSAGE) or {}
    msg_content = msg.get(_F_CONTENT)
    if not isinstance(msg_content, list):
        return
    for block in msg_content:
        if not isinstance(block, dict):
            continue
        if block.get(_F_TYPE) != _BLOCK_TYPE_TOOL_RESULT:
            continue
        tool_use_id = block.get("tool_use_id", "?")
        content = block.get(_F_CONTENT, "")
        if isinstance(content, list):
            parts = [
                c.get(_FIELD_TEXT, "")
                for c in content
                if isinstance(c, dict) and c.get(_F_TYPE) == _BLOCK_TYPE_TEXT
            ]
            text = "\n".join(parts)
        elif isinstance(content, str):
            text = content
        else:
            text = str(content)
        if text:
            log.info("[%s] Tool result (%s): %s", task_label, tool_use_id, text[:_LOG_TRUNC_LONG])


def _log_result_event(log: logging.Logger, task_label: str, ev: dict) -> None:
    subtype = ev.get(_F_SUBTYPE, "")
    duration = ev.get("duration_ms", 0)
    turns = ev.get("num_turns", 0)
    result_text = (ev.get(_F_RESULT) or "")[:_LOG_TRUNC_MED]
    log.info(
        "[%s] Result: %s (%ds, %d turns) %s",
        task_label, subtype, duration // 1000, turns, result_text,
    )


def _log_system_event(log: logging.Logger, task_label: str, ev: dict) -> None:
    subtype = ev.get(_F_SUBTYPE, "")
    if subtype == _SYS_INIT:
        log.info(
            "[%s] Session started (cwd=%s session_id=%s model=%s)",
            task_label,
            ev.get("cwd", "?"),
            ev.get(_F_SESSION_ID, "?"),
            ev.get("model", "?"),
        )
    elif subtype == _SYS_API_RETRY:
        log.warning(
            "[%s] API retry attempt=%s/%s delay=%sms error=%s status=%s",
            task_label,
            ev.get("attempt"), ev.get("max_retries"),
            ev.get("retry_delay_ms"), ev.get("error"), ev.get("error_status"),
        )
    elif subtype == _SYS_COMPACT_BOUNDARY:
        meta = ev.get("compact_metadata", {})
        log.info(
            "[%s] Conversation compacted (trigger=%s pre_tokens=%s)",
            task_label, meta.get("trigger"), meta.get("pre_tokens"),
        )
    elif subtype == _SYS_TASK_STARTED:
        log.info("[%s] Subagent started: %s", task_label, ev.get("description", ""))
    elif subtype == _SYS_TASK_NOTIFICATION:
        log.info(
            "[%s] Subagent %s: %s",
            task_label, ev.get("status", ""), ev.get("summary", ""),
        )


def _log_rate_limit_event(log: logging.Logger, task_label: str, ev: dict) -> None:
    info = ev.get("rate_limit_info", {})
    log.warning(
        "[%s] Rate limit: tokens %s/%s (resets %s) requests %s/%s",
        task_label,
        info.get("tokens_remaining"),
        info.get("tokens_limit"),
        info.get("tokens_reset_date"),
        info.get("requests_remaining"),
        info.get("requests_limit"),
    )


# Dispatch table — keep this in sync with the ``_T_*`` constants above.
# A missing handler is fine: events of types we don't render are silently
# dropped (claude may add new types in future versions).
_EVENT_LOGGERS: dict[str, Callable[[logging.Logger, str, dict], None]] = {
    _T_ASSISTANT: _log_assistant_event,
    _T_USER: _log_user_event,
    _T_RESULT: _log_result_event,
    _T_SYSTEM: _log_system_event,
    _T_RATE_LIMIT: _log_rate_limit_event,
}


def _log_claude_event(log: logging.Logger, task_label: str, ev: dict) -> None:
    """
    Log a pre-parsed event from Claude Code.

    Tolerates the shape differences between stream-json (stdout) and the
    session jsonl: ``message`` may be missing or ``None``, ``content``
    may be a string (initial prompt in jsonl) or a list, and individual
    blocks may not be dicts. A single off-spec event must NOT raise —
    otherwise it kills ``_tail_jsonl``'s loop and silences the bridge
    log for the rest of the run (incident 7e002960: 93 parent-jsonl
    events lost because the first one was a string-content user event).
    """
    handler = _EVENT_LOGGERS.get(ev.get(_F_TYPE, ""))
    if handler is not None:
        handler(log, task_label, ev)


def _has_assistant_content(ev: dict) -> bool:
    """
    True iff an ``assistant`` event carries any content blocks.

    Used by the stdout-EOF fallback in ``_execute_claude_skill`` to
    distinguish:
    - claude produced real output then exited (success-on-EOF)
    - claude crashed before producing anything (error-on-EOF)

    Liberal definition: any non-empty content list counts (text,
    tool_use, thinking, etc.). A claude run that emitted even a single
    tool call is doing real work and an EOF after that should be treated
    as a normal completion modulo the missing ``result`` event.
    """
    msg = ev.get(_F_MESSAGE) or {}
    content = msg.get(_F_CONTENT)
    if isinstance(content, list):
        return len(content) > 0
    if isinstance(content, str):
        return bool(content.strip())
    return False


def _process_jsonl_chunk(
    chunk: bytes,
    log: logging.Logger,
    task_label: str,
    ctx: _RunContext | None = None,
) -> None:
    """
    Route every event in a jsonl chunk through ``_log_claude_event``.

    The session jsonl at
    ``CLAUDE_CONFIG_DIR/projects/<derived-cwd>/<session_id>.jsonl`` is
    the SOLE source of bridge-log content. stream-json on stdout is
    used only for flow control (system/init → session id, result →
    completion event) and is intentionally not logged: claude buffers
    stdout in batches and an out-of-order arrival between channels
    used to silence jsonl events via uuid-based deduplication
    (incident pipeline 05bdd13d: bridge log empty 5+ minutes while
    jsonl grew by 300+ KB). Single source of truth → no race.

    When ``ctx`` is provided, also updates ``ctx.had_assistant_turn``
    so the stdout-EOF fallback in ``_execute_claude_skill`` can tell
    success-after-EOF from crash-after-EOF. The jsonl path is more
    reliable than stdout for this (stdout can be swallowed by buffering
    on abrupt exit; jsonl is fsynced by claude as it goes).
    """
    for raw_line in chunk.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Defense-in-depth around _log_claude_event: even with shape
        # guards in place, an unknown future event variant must not
        # poison the rest of the chunk (or the whole tail loop). Log
        # the exception and move on — never propagate up.
        try:
            _log_claude_event(log, task_label, ev)
        except Exception as exc:
            log.warning(
                "[%s] _log_claude_event failed on event type=%s: %s",
                task_label, ev.get(_F_TYPE), exc,
            )
        # Track assistant-turn presence for the stdout-EOF fallback.
        # Done after the logging dispatch so a buggy logger can't mask
        # the flag update.
        if (
            ctx is not None
            and not ctx.had_assistant_turn
            and ev.get(_F_TYPE) == _T_ASSISTANT
            and _has_assistant_content(ev)
        ):
            ctx.had_assistant_turn = True


def _parse_extra_args(extra_args: str) -> dict[str, str]:
    """Parse space-separated key=value pairs from an extra_args string."""
    parsed: dict[str, str] = {}
    for part in (extra_args or "").split():
        k, sep, v = part.partition("=")
        if sep:
            parsed[k] = v
    return parsed


def _resolve_model(req_model: str) -> str:
    """
    Pick the effective ``--model`` value for a request.

    Per-request ``req.model`` wins; otherwise fall back to the bridge-wide
    ``CLAUDE_BRIDGE_MODEL`` default. Empty result means "no ``--model`` flag".
    """
    return req_model or CLAUDE_BRIDGE_MODEL


def _build_claude_cmd(prompt: str, model: str) -> list[str]:
    """
    Assemble the ``claude -p`` argv. Adds ``--model`` only when set.

    ``model`` is already validated by ``AnalyzeRequest`` (no leading ``-``),
    so it cannot inject an extra CLI flag.
    """
    cmd = [CLAUDE_PATH, "-p", prompt]
    if model:
        cmd += ["--model", model]
    cmd += [
        "--dangerously-skip-permissions",
        "--verbose",
        "--output-format", "stream-json",
    ]
    return cmd


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


async def _stream_stderr(stream: asyncio.StreamReader, ctx: _RunContext) -> None:
    """Capture claude stderr to ``ctx.stderr_lines`` and mirror to the bridge log."""
    async for line in stream:
        ctx.stderr_lines.append(line)
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            ctx.log.warning("[%s] stderr: %s", ctx.task_label, text)


async def _stream_stdout(stream: asyncio.StreamReader, ctx: _RunContext) -> None:
    """
    Drive flow control + log stream-json-only event types.

    Assistant/user turns are deliberately NOT logged here — those arrive
    on stdout in delayed batches and would race the live jsonl tail.
    They're handled exclusively by ``_tail_jsonl``. What this DOES log:
    types that don't appear in the session jsonl at all (``system``,
    ``result``, ``rate_limit_event``) so the operator still sees session
    start, API-retry warnings and the final ``Result: ...`` line.

    Side-effects on ``ctx``:
    - ``ctx.session_id`` / ``ctx.session_id_known`` from ``system/init``
    - ``ctx.payload`` / ``ctx.result_event`` from ``result``
    - ``ctx.had_assistant_turn`` from any non-empty ``assistant`` event
      (used by the EOF fallback below)
    - ``ctx.stdout_done`` is set in the ``finally`` block — this is the
      strongest "claude tree is fully done" signal because subagents
      inherit fd1 and the pipe only EOFs once all of them are gone.
    """
    # Read line-by-line via the iterator protocol (not ``async for``) so a
    # single line that overruns the StreamReader buffer can be dropped without
    # aborting the whole stream — ``readline`` discards the offending line and
    # the next ``__anext__`` resumes on the following line.
    stdout_iter = aiter(stream)
    try:
        while True:
            try:
                line = await anext(stdout_iter)
            except StopAsyncIteration:
                break  # genuine pipe EOF — every fd1 holder is gone
            except (ValueError, asyncio.LimitOverrunError):
                # A single stream-json line overran the reader buffer even at
                # ``_STREAM_READER_LIMIT`` (a giant tool_result echo). Drop it
                # and keep reading: this line must not kill flow control or
                # hide the small ``result`` event that follows it (incident
                # pipeline f4dbe4aa). The authoritative triage output is the
                # on-disk result file, not this log channel.
                ctx.stdout_lines_dropped += 1
                ctx.log.warning(
                    "[%s] dropping stdout line #%d that overran the %d-byte "
                    "reader limit; continuing to stream",
                    ctx.task_label, ctx.stdout_lines_dropped, _STREAM_READER_LIMIT,
                )
                continue
            except asyncio.CancelledError:
                # Normal teardown — re-raise so the task is marked cancelled;
                # the ``finally`` still fires ``stdout_done``.
                raise
            except Exception:
                # Any OTHER reader error is unexpected. Record it so the EOF
                # fallback in ``_execute_claude_skill`` does not mistake the
                # ``stdout_done`` signal below for a clean EOF + false success.
                # Log loudly — never swallow silently.
                ctx.stdout_reader_failed = True
                ctx.log.exception(
                    "[%s] stdout reader crashed; treating as failure, not EOF",
                    ctx.task_label,
                )
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            try:
                ev = json.loads(text)
            except json.JSONDecodeError:
                continue
            ev_type = ev.get(_F_TYPE, "")
            if ev_type in _STDOUT_ONLY_LOG_TYPES:
                _log_claude_event(ctx.log, ctx.task_label, ev)
            if ev_type == _T_ASSISTANT and not ctx.had_assistant_turn and _has_assistant_content(ev):
                ctx.had_assistant_turn = True
            if ev_type == _T_SYSTEM and ev.get(_F_SUBTYPE) == _SYS_INIT:
                sid = ev.get(_F_SESSION_ID, "")
                if sid and not ctx.session_id_known.is_set():
                    ctx.session_id = sid
                    ctx.session_id_known.set()
            if ev_type == _T_RESULT and ctx.payload is None:
                ctx.payload = _payload_from_result_event(ev)
                ctx.result_event.set()
    finally:
        # Signal EOF / drain-complete unconditionally — even on
        # CancelledError. ``_execute_claude_skill`` uses this as the
        # defence-in-depth completion signal when proc.wait() is wedged
        # by the asyncio child-watcher race with tini.
        ctx.stdout_done.set()


async def _watch_result_file(ctx: _RunContext) -> None:
    """Complete when ``ctx.result_file_path`` appears on disk."""
    if ctx.result_file_path is None:
        # No path configured — sleep until cancelled.
        await asyncio.sleep(TRIAGE_TIMEOUT + 1)
        return
    while True:
        if ctx.result_file_path.exists():
            ctx.log.info(
                "[%s] Result file detected on disk: %s",
                ctx.task_label, ctx.result_file_path,
            )
            return
        await asyncio.sleep(_RESULT_POLL_INTERVAL)


def _valid_result_file_payload(ctx: _RunContext) -> CallbackPayload | None:
    """
    Return a success payload iff a valid, non-empty JSON result file is on disk.

    The result file is the authoritative triage artifact written by the skill;
    stdout is only the live log channel. When stdout completion is ambiguous
    (an unexpected reader crash), consult the file before declaring failure so
    a run that genuinely produced output is not reported as error. The 5-second
    ``_watch_result_file`` poll can lag a crash that fires ``stdout_done``
    immediately, so this is a synchronous last look (incident pipeline f4dbe4aa).
    """
    path = ctx.result_file_path
    if path is None or not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return CallbackPayload(status="success")


async def _tail_jsonl(ctx: _RunContext) -> None:
    """
    Tail every jsonl file claude writes for this session.

    Claude writes turns to multiple files:
      <projects>/<derived-cwd>/<session_id>.jsonl       parent
      <projects>/<derived-cwd>/<session_id>/subagents/agent-*.jsonl
                                                        per Task subagent
    For skills that delegate work to sub-agents (incident 3e565a58
    running aist-full-security-review) almost all the real
    Read/Bash/Grep/thinking happens INSIDE the subagent files. The
    parent jsonl only records the Task tool_use + the aggregated
    result. Watching only the parent leaves operators with
    "Subagent started/completed" headers and nothing in between.

    Subagents are spawned lazily, so we rescan ``subagents/`` on every
    tick rather than once at startup. Each tailed file carries its own
    offset and a per-file task_label so log lines attribute correctly
    to parent vs each subagent.

    Sole bridge-log writer — formats every event via
    ``_log_claude_event``. Earlier iterations of this code used
    ``log.debug`` (silently dropped by basicConfig level=INFO, incident
    a2a7ed26) and a uuid dedup that silenced jsonl events whenever
    stdout flushed first (incident 05bdd13d).
    """
    await ctx.session_id_known.wait()
    if not ctx.session_id:
        return

    parent_jsonl = await _find_parent_jsonl(ctx)
    if parent_jsonl is None:
        return

    ctx.log.info("[%s] Tailing session jsonl: %s", ctx.task_label, parent_jsonl)

    subagents_dir = parent_jsonl.parent / parent_jsonl.stem / _JSONL_SUBAGENTS_DIR
    offsets: dict[Path, int] = {parent_jsonl: 0}
    labels: dict[Path, str] = {parent_jsonl: ctx.task_label}

    while True:
        # Outer guard: this loop must survive ANY error other than
        # CancelledError (which is BaseException, not caught by
        # Exception). Without it, a single anomaly (file system blip,
        # pathlib edge case, future code tweak that raises) silently
        # kills the tail and the bridge log goes dark for the rest of
        # the run. The exception is logged via log.exception and the
        # next tick proceeds normally — defense-in-depth, NOT
        # error-hiding.
        try:
            _discover_new_subagents(subagents_dir, offsets, labels, ctx)
            for path, offset in list(offsets.items()):
                _read_jsonl_delta(path, offset, offsets, labels, ctx)
        except Exception:
            ctx.log.exception(
                "[%s] tail loop iteration failed; continuing",
                ctx.task_label,
            )

        await asyncio.sleep(_JSONL_POLL_INTERVAL)


async def _find_parent_jsonl(ctx: _RunContext) -> Path | None:
    """Wait up to ``_JSONL_FIND_MAX_WAIT`` for the parent session jsonl."""
    retries = int(_JSONL_FIND_MAX_WAIT / _JSONL_POLL_INTERVAL)
    for _ in range(retries):
        candidates = list(
            ctx.claude_config_dir.glob(
                f"{_JSONL_PROJECTS_DIR}/**/{ctx.session_id}.jsonl",
            ),
        )
        if candidates:
            return candidates[0]
        await asyncio.sleep(_JSONL_POLL_INTERVAL)
    ctx.log.warning(
        "[%s] Session jsonl not found for session_id=%s within %ds",
        ctx.task_label, ctx.session_id, _JSONL_FIND_MAX_WAIT,
    )
    return None


def _discover_new_subagents(
    subagents_dir: Path,
    offsets: dict[Path, int],
    labels: dict[Path, str],
    ctx: _RunContext,
) -> None:
    """
    Pick up jsonls for sub-agents that have appeared since the last tick.

    Glob on a missing directory returns empty without raising — safe before
    any Task tool fires.
    """
    for sub_path in subagents_dir.glob("*.jsonl"):
        if sub_path in offsets:
            continue
        offsets[sub_path] = 0
        labels[sub_path] = f"{ctx.task_label} {sub_path.stem}"
        ctx.log.info("[%s] Tailing subagent jsonl: %s", ctx.task_label, sub_path)


def _read_jsonl_delta(
    path: Path,
    offset: int,
    offsets: dict[Path, int],
    labels: dict[Path, str],
    ctx: _RunContext,
) -> None:
    """
    Read everything appended since ``offset``, commit only up to the last
    complete line, and forward to ``_process_jsonl_chunk``.

    Holds the trailing partial bytes back: POSIX ``write(2)`` on a
    regular file is not atomic for ``len > 1``, so a poll that lands
    mid-write reads a partial line. Advancing the offset past those
    bytes would orphan both halves — ``json.loads`` would fail on the
    prefix this tick and on the suffix next tick, dropping the event.
    """
    try:
        size = path.stat().st_size
        if size <= offset:
            return
        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        # File may be momentarily unreadable mid-write — next tick will
        # retry from the same offset.
        return

    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return  # entire chunk is one in-progress line; wait for more
    complete = chunk[: last_nl + 1]
    offsets[path] = offset + len(complete)
    _process_jsonl_chunk(complete, ctx.log, labels[path], ctx=ctx)


async def _cleanup_run_tasks(
    bg_tasks: tuple[asyncio.Task, ...],
    drain_tasks: tuple[asyncio.Task, ...],
) -> None:
    """
    Cancel background tasks; drain stdout/stderr so we don't leak coroutines.

    Drain tasks get up to 5 seconds to see EOF naturally before being
    cancelled — the pgroup-kill in ``_terminate_subprocess`` should have
    already closed the pipes. Background tasks (jsonl tailer, result
    waiter, ...) are cancelled outright; they hold no external state we
    care about preserving.

    All exceptions are swallowed: cleanup is best-effort and MUST NOT
    propagate. If it did, the caller's ``finally`` would not finish, the
    callback to AIST would never POST, and the pipeline would hang in
    ``WAITING_RESULT_FROM_AI`` until the bridge restarts. The drain
    tasks themselves are logging-only; their failures are useful as
    breadcrumbs but never load-bearing.
    """
    for bg_task in bg_tasks:
        if not bg_task.done():
            bg_task.cancel()
            with contextlib.suppress(BaseException):
                await bg_task
    for drain_task in drain_tasks:
        if drain_task.done():
            continue
        try:
            await asyncio.wait_for(asyncio.shield(drain_task), timeout=5)
        except TimeoutError:
            drain_task.cancel()
            with contextlib.suppress(BaseException):
                await drain_task
        except Exception:
            # Drain coroutine raised mid-loop (e.g. logging filter bug).
            # Cancel idempotently so it doesn't dangle, then move on —
            # propagating would block the callback POST upstream.
            drain_task.cancel()
            with contextlib.suppress(BaseException):
                await drain_task


async def _execute_claude_skill(req: AnalyzeRequest) -> CallbackPayload:
    """
    Run claude -p in a subprocess and return its CallbackPayload.

    Completion is detected via three independent signals (whichever fires first):
    1. stream-json ``result`` event on stdout — the normal path.
    2. Result file appears on disk — fallback when stdout goes silent
       mid-session (observed when Claude uses subagents).
    3. Process exits — final fallback; rc=0 → success, rc≠0 → error.

    SIGTERM is sent (to the whole pgroup) after a result is detected;
    SIGKILL follows after ``POST_RESULT_GRACE`` seconds if the process
    hasn't exited. ``TRIAGE_TIMEOUT`` is the hard ceiling. The ``finally``
    block runs cleanup unconditionally — on success, on error, and on
    cancellation — so no background task or pipe FD leaks past return.
    """
    prompt = _build_skill_prompt(req.skill_name, req.project_id, req.source_path, req.extra_args)

    # Parse result file location from extra_args — our own protocol set by agent_bridge_runner.
    parsed_args = _parse_extra_args(req.extra_args)
    output_path = parsed_args.get("output_path", "")
    result_filename = parsed_args.get("result_filename", "")
    result_file_path = (
        Path(output_path) / result_filename if output_path and result_filename else None
    )

    # Per-run CLAUDE_CONFIG_DIR with a UUID suffix so concurrent invocations
    # for the same ``project_id`` (retry storms, upstream races) do not
    # share session jsonl files or stomp each other's cleanup. The
    # finally-block ``shutil.rmtree`` only ever touches paths derived
    # from this run's ``run_id``.
    run_id = uuid.uuid4().hex[:8]
    claude_config_dir = _CLAUDE_RUNS_DIR / f"{req.project_id}-{run_id}"
    claude_config_dir.mkdir(parents=True, exist_ok=True)
    # Inherit container env (PATH/HOME/LANG) and overlay the per-request
    # subprocess_env coming from the caller. Generic mechanism: the
    # bridge does not know which keys are Claude credentials vs unrelated
    # env vars — it just forwards what the caller sent. Task 9 guarantees
    # the container env itself never contains agent credentials, so the
    # inherited keys are safe.
    subprocess_env = {**os.environ, "CLAUDE_CONFIG_DIR": str(claude_config_dir)}
    # Defence-in-depth redaction seed: include any token-shaped value
    # that leaked into the bridge container env, on top of the values
    # the caller explicitly passed through ``req.subprocess_env``.
    secret_values: list[str] = _scan_env_for_anthropic_secrets()
    for var_name, secret in req.subprocess_env.items():
        raw_value = secret.get_secret_value()
        subprocess_env[var_name] = raw_value
        if raw_value:
            secret_values.append(raw_value)

    cmd = _build_claude_cmd(prompt, _resolve_model(req.model))
    task_label = _task_label(req)

    log = logging.getLogger(f"aist-triage-bridge.task.{req.project_id}")
    log.propagate = True
    # Per-run redaction: masks any literal secret value present in this
    # request's subprocess_env from every log line emitted via ``log``.
    # Defence-in-depth — claude CLI is observed to echo rejected tokens
    # in auth-error stderr lines (Task 4 e2e test).
    redacting_filter = _RedactingFilter(secret_values) if secret_values else None
    if redacting_filter is not None:
        log.addFilter(redacting_filter)
    file_handler = _open_pipeline_log_handler(req.project_id, log=log)
    if file_handler is not None:
        log.addHandler(file_handler)

    log.info(
        "Starting claude -p for %s cwd=%s result_file=%s model=%s",
        task_label, AIST_WORKING_DIR, result_file_path or "<none>",
        _resolve_model(req.model) or "<cli-default>",
    )

    ctx = _RunContext(
        log=log,
        task_label=task_label,
        claude_config_dir=claude_config_dir,
        result_file_path=result_file_path,
    )

    result: CallbackPayload | None = None
    proc: asyncio.subprocess.Process | None = None
    bg_tasks: tuple[asyncio.Task, ...] = ()
    drain_tasks: tuple[asyncio.Task, ...] = ()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=AIST_WORKING_DIR,
            env=subprocess_env,
            # Applies to both the stdout and stderr StreamReaders. stream-json
            # lines (tool_result echoes, large diffs) routinely exceed the
            # default 64 KiB limit; see ``_STREAM_READER_LIMIT``.
            limit=_STREAM_READER_LIMIT,
            # New session/pgroup: lets _signal_pgroup take down the CLI
            # plus every Bash/Agent subprocess Claude spawns. Without
            # this, a SIGKILL on proc.pid orphans grandchildren that
            # keep the stdout/stderr pipes open and hang the drain
            # coroutines (incident a2a7ed26).
            start_new_session=True,
        )

        stdout_task = asyncio.create_task(_stream_stdout(proc.stdout, ctx))
        stderr_task = asyncio.create_task(_stream_stderr(proc.stderr, ctx))
        proc_wait_task = asyncio.create_task(proc.wait())
        result_wait_task = asyncio.create_task(ctx.result_event.wait())
        file_watch_task = asyncio.create_task(_watch_result_file(ctx))
        jsonl_tail_task = asyncio.create_task(_tail_jsonl(ctx))
        # Fourth completion signal: stdout pipe EOF. Strongest available
        # "claude tree is fully done" signal — subagents inherit fd1 so
        # the pipe only EOFs after every process in the group exits.
        # Defence-in-depth against the asyncio ThreadedChildWatcher race
        # with tini that wedges proc.wait() (incident pipeline 4a1912d7).
        stdout_done_task = asyncio.create_task(ctx.stdout_done.wait())

        drain_tasks = (stdout_task, stderr_task)
        bg_tasks = (file_watch_task, jsonl_tail_task, result_wait_task, proc_wait_task, stdout_done_task)

        done, _pending = await asyncio.wait(
            {result_wait_task, proc_wait_task, file_watch_task, stdout_done_task},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=TRIAGE_TIMEOUT,
        )

        if result_wait_task in done or file_watch_task in done:
            if result_wait_task in done:
                result = ctx.payload or CallbackPayload(
                    status="error", detail="claude -p result event missing payload",
                )
                log.info(
                    "claude -p result event received for %s status=%s; terminating (pid=%s)",
                    task_label, result.status, proc.pid,
                )
            else:
                log.info(
                    "Result file appeared for %s (stdout was silent); treating as success (pid=%s)",
                    task_label, proc.pid,
                )
                result = CallbackPayload(status="success")
            await _terminate_subprocess(proc, proc_wait_task, log, task_label)

        elif proc_wait_task in done:
            rc = proc.returncode
            if rc == 0:
                log.info("claude -p succeeded for %s (no result event, exit=0)", task_label)
                result = CallbackPayload(status="success")
            else:
                err = b"".join(ctx.stderr_lines).decode("utf-8", errors="replace")[:_LOG_TRUNC_LONG]
                log.error("claude -p failed for %s exit=%s", task_label, rc)
                result = CallbackPayload(status="error", detail=err or f"claude -p exit={rc}")

        elif stdout_done_task in done and ctx.stdout_reader_failed:
            # ``stdout_done`` fired because ``_stream_stdout`` CRASHED, not
            # because the pipe reached a clean EOF. Its "claude tree is fully
            # done" meaning does NOT hold here: claude may still be alive and
            # mid-work (incident pipeline f4dbe4aa — a >64 KiB tool_result line
            # overran the reader, the live process was killed, and the empty
            # run was reported as success). No result event was seen, so this
            # run failed — report error and tear the still-running CLI down
            # normally (never the ECHILD fast-path, which assumes the child is
            # already gone).
            recovered = _valid_result_file_payload(ctx)
            if recovered is not None:
                # claude had already written its authoritative result file
                # before the reader died — recover the real outcome instead
                # of failing the run on a log-channel crash.
                log.warning(
                    "claude -p stdout reader failed for %s pid=%s, but a valid "
                    "result file is present — recovering as success",
                    task_label, proc.pid,
                )
                result = recovered
            else:
                log.error(
                    "claude -p stdout reader failed for %s pid=%s without a result "
                    "event or valid result file; reporting error",
                    task_label, proc.pid,
                )
                result = CallbackPayload(
                    status="error",
                    detail="claude -p stdout reader crashed before producing a "
                           "result event or valid result file; triage incomplete",
                )
            await _terminate_subprocess(proc, proc_wait_task, log, task_label)

        elif stdout_done_task in done:
            # Stdout EOF arrived but neither a result event nor proc exit
            # was observed. This is the asyncio child-watcher race signature:
            # claude actually exited (and tini reaped it), the pipe is in
            # EOF, but ``proc.wait()`` is wedged because the SIGCHLD was
            # delivered to tini before asyncio could observe it. The pipe
            # EOF is authoritative — every fd1-holding process is gone,
            # so it is safe to terminate the run now rather than spinning
            # for ``TRIAGE_TIMEOUT`` more seconds.
            log.warning(
                "claude -p stdout EOF for %s pid=%s without result event; "
                "had_assistant_turn=%s — treating as %s (asyncio child-watcher "
                "race fallback)",
                task_label, proc.pid, ctx.had_assistant_turn,
                "success" if ctx.had_assistant_turn else "error",
            )
            if ctx.had_assistant_turn:
                result = CallbackPayload(status="success")
            else:
                result = CallbackPayload(
                    status="error",
                    detail="claude -p exited without producing any assistant output",
                )
            # Fast-path: if the OS confirms the child is already reaped
            # (tini got there first; ``ECHILD`` from waitpid), skip the
            # SIGTERM/SIGKILL dance in ``_terminate_subprocess`` which
            # would otherwise spin for POST_RESULT_GRACE*2 seconds
            # waiting for asyncio's wedged proc.wait() to return. This
            # is what makes the EOF fallback take seconds rather than
            # tens of seconds.
            process_already_reaped = False
            try:
                wait_pid, _ = os.waitpid(proc.pid, os.WNOHANG)  # noqa: ASYNC222
                # Nonzero pid → we just reaped it; 0 → still alive
                # (rare false EOF — fall through to normal terminate).
                process_already_reaped = wait_pid != 0
            except ChildProcessError:
                # ECHILD — already reaped by tini, exactly the 4a1912d7 case.
                process_already_reaped = True
            except OSError as exc:
                log.warning(
                    "waitpid(%s, WNOHANG) failed for %s: %s — falling back "
                    "to terminate", proc.pid, task_label, exc,
                )
            if process_already_reaped and not proc_wait_task.done():
                proc_wait_task.cancel()
            elif not process_already_reaped:
                await _terminate_subprocess(proc, proc_wait_task, log, task_label)

        else:
            last_stderr = (
                b"".join(ctx.stderr_lines[-20:])
                .decode("utf-8", errors="replace")[:_LOG_TRUNC_MED]
            )
            log.error(
                "claude -p timed out after %ds for %s pid=%s returncode=%s; last stderr: %s",
                TRIAGE_TIMEOUT, task_label, proc.pid, proc.returncode, last_stderr or "<empty>",
            )
            await _terminate_subprocess(proc, proc_wait_task, log, task_label)
            result = CallbackPayload(
                status="error",
                detail=f"claude -p timed out after {TRIAGE_TIMEOUT}s",
            )

    except Exception:
        log.exception("claude -p crashed for %s", task_label)
        result = CallbackPayload(status="error", detail="bridge internal error")
    finally:
        # ── Sync cleanup ────────────────────────────────────────────────
        # Runs unconditionally — including during CancelledError
        # propagation, where any subsequent ``await`` may be interrupted.
        # Order: kill the pgroup, free the parent-side pipe FDs via
        # transport.close, drop the file handler, wipe the per-run
        # config dir.
        #
        # Cancellation path (handler cancelled, e.g. client disconnect):
        # ``_terminate_subprocess`` never ran. Killing the whole pgroup
        # here prevents grandchildren from surviving and holding the
        # pipes (regression of incident a2a7ed26 in cancellation edge
        # case).
        if proc is not None and proc.returncode is None:
            _signal_pgroup(proc.pid, signal.SIGKILL)
        if proc is not None:
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
        if file_handler is not None:
            log.removeHandler(file_handler)
            file_handler.close()
        if redacting_filter is not None:
            log.removeFilter(redacting_filter)
        shutil.rmtree(claude_config_dir, ignore_errors=True)

        # ── Async cleanup ───────────────────────────────────────────────
        # Cancel every task we created — covers success, exception, AND
        # cancellation paths uniformly. Without this the except path
        # used to leak 5 of 6 background tasks per crashed request
        # until lifespan shutdown.
        #
        # On cancellation this ``await`` may itself be interrupted; the
        # critical sync cleanup above has already freed the pipe FDs by
        # then, so the worst case is bg_tasks dangling briefly until
        # uvicorn shutdown collects them — not a correctness issue.
        #
        # All non-cancellation exceptions are swallowed here: this finally
        # block runs BEFORE the callback POST in ``_run_claude_skill``,
        # and we cannot let cleanup break the callback contract — the
        # pipeline upstream would hang in WAITING_RESULT_FROM_AI until
        # the bridge container restarts. ``_cleanup_run_tasks`` already
        # swallows its own task-level exceptions; the catch here is a
        # last-resort guard against future regressions.
        if bg_tasks or drain_tasks:
            try:
                await asyncio.wait_for(
                    _cleanup_run_tasks(bg_tasks, drain_tasks), timeout=30,
                )
            except TimeoutError:
                log.error(
                    "Cleanup timed out for %s; abandoning background tasks",
                    task_label,
                )
            except Exception:
                log.exception(
                    "Cleanup itself raised for %s; abandoning background tasks",
                    task_label,
                )

    # Falls through to here only if try/except set ``result``. Cancellation
    # propagates as BaseException through finally and never reaches this.
    return result if result is not None else CallbackPayload(
        status="error", detail="bridge internal error",
    )


async def _run_claude_skill(req: AnalyzeRequest) -> None:
    """
    Execute the skill and POST the result to ``req.callback_url`` (if set).

    Used by the async ``/analyze`` endpoint where the caller registers a
    callback URL and continues without waiting. ``/analyze-sync`` callers
    use ``_execute_claude_skill`` directly instead.
    """
    task_label = _task_label(req)
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
    """
    Schedule the claude skill coroutine and track it.

    Strong-references the task in ``_running_tasks`` so the loop doesn't
    GC it mid-flight (canonical asyncio pattern), then auto-discards on
    completion.
    """
    task = asyncio.create_task(_run_claude_skill(req))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)


# ── FastAPI app ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Operator-visible startup warning. /analyze-sync (used by SAST
    # analyzers) does not need the service token, so we do NOT fail-fast
    # here — but /analyze with a callback_url will 401 silently at AIST
    # without it. The per-request guard below upgrades that to a clean
    # 503 so the caller knows immediately.
    if not AIST_SERVICE_TOKEN:
        logger.warning(
            "AIST_SERVICE_TOKEN is not set. /analyze requests carrying a "
            "callback_url will be rejected with HTTP 503 until the env is "
            "configured. /analyze-sync is unaffected.",
        )
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
    if req.callback_url and not AIST_SERVICE_TOKEN:
        # Without the service token AIST's LocalTriageCompleteAPI (which
        # has ``IsAuthenticated``) returns 401 to our callback, the
        # bridge logs the failure but the pipeline stays in
        # WAITING_RESULT_FROM_AI forever. Fail loudly at request time
        # so the caller can either set the token or omit callback_url.
        raise HTTPException(
            status_code=503,
            detail=(
                "bridge has no AIST_SERVICE_TOKEN configured; "
                "callbacks to AIST would fail authentication. "
                "Set the env var or omit callback_url."
            ),
        )
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
