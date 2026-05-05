"""
Unit tests for aist-triage-bridge.

Runs standalone: `python -m unittest tools/aist-triage-bridge/test_main.py`
(with ``tools/aist-triage-bridge`` on ``sys.path``). The bridge ships in its
own container so it is not picked up by the Django test runner.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent))

import main
from main import AnalyzeRequest


class ProcessJsonlChunkTests(unittest.TestCase):

    """
    ``_process_jsonl_chunk`` is the sole writer of bridge-log content.
    It must parse every jsonl line, ignore garbage, and forward each
    event to ``_log_claude_event`` regardless of any prior state — there
    is no dedup against stdout, by design (see incident 05bdd13d).
    """

    def setUp(self):
        self._log = logging.getLogger("test-process-jsonl")
        self._calls: list[dict] = []
        self._patch = patch(
            "main._log_claude_event",
            side_effect=lambda _log, _label, ev: self._calls.append(ev),
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_each_jsonl_line_is_routed_through_log_claude_event(self):
        chunk = (
            b'{"type":"assistant","uuid":"u1","message":{"content":[]}}\n'
            b'{"type":"user","uuid":"u2","message":{"content":[]}}\n'
        )
        main._process_jsonl_chunk(chunk, self._log, "label")
        self.assertEqual([ev["uuid"] for ev in self._calls], ["u1", "u2"])

    def test_does_not_dedup_against_prior_invocations(self):
        # No state shared between runs — dedup was the bug, not a feature.
        chunk = b'{"type":"assistant","uuid":"u1","message":{"content":[]}}\n'
        main._process_jsonl_chunk(chunk, self._log, "label")
        main._process_jsonl_chunk(chunk, self._log, "label")
        self.assertEqual(len(self._calls), 2)

    def test_invalid_json_lines_are_skipped_silently(self):
        chunk = b'not-json\n{"type":"assistant","uuid":"u1","message":{"content":[]}}\n'
        main._process_jsonl_chunk(chunk, self._log, "label")
        self.assertEqual([ev["uuid"] for ev in self._calls], ["u1"])

    def test_blank_lines_are_skipped(self):
        chunk = b"\n  \n\n"
        main._process_jsonl_chunk(chunk, self._log, "label")
        self.assertEqual(self._calls, [])


class LogClaudeEventResilienceTests(unittest.TestCase):

    """
    ``_log_claude_event`` must tolerate every shape claude has been
    observed to emit — JSONL events have message.content as a string
    (initial prompt), missing message, None message, non-dict blocks.
    A single off-spec event must NOT raise: incident 7e002960 — a
    single string-content user event killed the tail coroutine and the
    next 92 jsonl events from a 5-minute run never reached the log.
    """

    def setUp(self):
        self._log = logging.getLogger("test-log-claude-event-resilience")

    def test_user_event_with_string_content_does_not_raise(self):
        # First user turn in a JSONL session: the initial prompt as a
        # raw string. Iterating it would yield characters, and the old
        # ``block.get(...)`` pattern then raised AttributeError.
        ev = {
            "type": "user", "uuid": "u",
            "message": {"role": "user", "content": "initial 30KB system prompt"},
        }
        main._log_claude_event(self._log, "label", ev)

    def test_assistant_event_with_string_content_does_not_raise(self):
        ev = {
            "type": "assistant", "uuid": "u",
            "message": {"content": "raw text not in blocks"},
        }
        main._log_claude_event(self._log, "label", ev)

    def test_missing_message_field_does_not_raise(self):
        main._log_claude_event(self._log, "label", {"type": "user", "uuid": "u"})
        main._log_claude_event(self._log, "label", {"type": "assistant", "uuid": "u"})

    def test_none_message_field_does_not_raise(self):
        main._log_claude_event(
            self._log, "label",
            {"type": "user", "uuid": "u", "message": None},
        )
        main._log_claude_event(
            self._log, "label",
            {"type": "assistant", "uuid": "u", "message": None},
        )

    def test_non_dict_blocks_in_content_array_are_skipped(self):
        ev = {
            "type": "assistant", "uuid": "u",
            "message": {"content": [
                None,
                "stray-string-in-blocks",
                42,
                {"type": "text", "text": "this one survives"},
            ]},
        }
        main._log_claude_event(self._log, "label", ev)

    def test_user_event_with_mixed_block_types_picks_up_tool_results(self):
        # Test that the loop actually keeps working past non-dict items
        # rather than just exiting silently.
        captured: list[tuple] = []
        with patch.object(self._log, "info",
                          side_effect=lambda *a: captured.append(a)):
            main._log_claude_event(
                self._log, "label",
                {"type": "user", "uuid": "u", "message": {"content": [
                    None,
                    {"type": "tool_result", "tool_use_id": "t1", "content": "hello"},
                    "garbage",
                    {"type": "tool_result", "tool_use_id": "t2", "content": "world"},
                ]}},
            )
        # Both tool_results must reach log.info, despite the garbage
        # interleaved between them.
        rendered = " ".join(str(args) for args in captured)
        self.assertIn("hello", rendered)
        self.assertIn("world", rendered)


class ProcessJsonlChunkResilienceTests(unittest.TestCase):

    """
    ``_process_jsonl_chunk`` MUST NOT propagate exceptions from one
    bad event — otherwise a single weird shape kills the tail
    coroutine for the rest of the run (incident 7e002960). This is
    defense-in-depth on top of the shape guards inside
    ``_log_claude_event`` itself.
    """

    def test_failure_on_one_event_does_not_abort_the_chunk(self):
        log = logging.getLogger("test-process-resilience")
        seen: list[dict] = []

        def boom_then_normal(_log, _label, ev):
            if ev.get("uuid") == "boom":
                msg = "simulated future schema drift"
                raise AttributeError(msg)
            seen.append(ev)

        with patch("main._log_claude_event", side_effect=boom_then_normal):
            chunk = (
                b'{"type":"assistant","uuid":"boom","message":{}}\n'
                b'{"type":"assistant","uuid":"after","message":{}}\n'
            )
            main._process_jsonl_chunk(chunk, log, "label")

        # Crucial: the second event still reached _log_claude_event
        # despite the first one raising. Without the wrapper, the
        # entire chunk (and the tail loop) would have died.
        self.assertEqual([ev["uuid"] for ev in seen], ["after"])


class FormatToolUseDetailTests(unittest.TestCase):

    """
    ``_format_tool_use_detail`` is the per-tool extraction routine
    factored out of ``_log_assistant_event`` for readability and unit
    testing. Each branch picks the most informative field for the
    operator; the ``else`` falls back to the truncated raw input.
    """

    def test_bash_prefers_description_then_falls_back_to_command(self):
        self.assertEqual(
            main._format_tool_use_detail("Bash", {"description": "list", "command": "ls -la"}),
            "list",
        )
        self.assertEqual(
            main._format_tool_use_detail("Bash", {"command": "ls -la"}),
            "ls -la",
        )

    def test_read_uses_file_path(self):
        self.assertEqual(
            main._format_tool_use_detail("Read", {"file_path": "/x.py"}),
            "/x.py",
        )

    def test_glob_and_grep_use_pattern(self):
        self.assertEqual(main._format_tool_use_detail("Glob", {"pattern": "**/*.py"}), "**/*.py")
        self.assertEqual(main._format_tool_use_detail("Grep", {"pattern": "TODO"}), "TODO")

    def test_agent_skill_task_prefer_description_then_skill(self):
        self.assertEqual(
            main._format_tool_use_detail("Agent", {"description": "explore auth"}),
            "explore auth",
        )
        self.assertEqual(
            main._format_tool_use_detail("Task", {"description": "explore", "skill": "x"}),
            "explore",
        )
        self.assertEqual(
            main._format_tool_use_detail("Skill", {"skill": "aist-x"}),
            "aist-x",
        )

    def test_edit_and_write_use_file_path(self):
        self.assertEqual(
            main._format_tool_use_detail("Edit", {"file_path": "/y"}),
            "/y",
        )
        self.assertEqual(
            main._format_tool_use_detail("Write", {"file_path": "/z"}),
            "/z",
        )

    def test_unknown_tool_falls_back_to_truncated_raw_input(self):
        # ``str(dict)`` is not pretty but at least keeps something visible.
        result = main._format_tool_use_detail("FuturisticNewTool", {"foo": "x" * 5000})
        self.assertLessEqual(len(result), main._LOG_TRUNC_SHORT)
        self.assertIn("foo", result)


class FinallyCleanupTests(unittest.IsolatedAsyncioTestCase):

    """
    Pin the contract of ``_execute_claude_skill``'s ``finally`` block:
    the pgroup-kill backstop runs whenever ``proc`` is still alive (e.g.
    cancellation path where ``_terminate_subprocess`` was skipped), and
    does NOT double-kill on the normal success path where
    ``_terminate_subprocess`` already brought the proc down.
    """

    async def asyncSetUp(self):
        self._orig_timeout = main.TRIAGE_TIMEOUT
        self._orig_grace = main.POST_RESULT_GRACE
        main.TRIAGE_TIMEOUT = 2
        main.POST_RESULT_GRACE = 1

    async def asyncTearDown(self):
        main.TRIAGE_TIMEOUT = self._orig_timeout
        main.POST_RESULT_GRACE = self._orig_grace

    async def test_finally_pgroup_kills_when_terminate_subprocess_was_skipped(self):
        """
        Regression for cancellation path: ``_terminate_subprocess``
        never runs, so without the finally backstop, ``proc`` would
        survive holding pipes (incident a2a7ed26 in cancellation
        edge case).
        """
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success"}\n'],
        )
        sig_calls: list[tuple] = []

        def track_and_drive(pid, sig):
            sig_calls.append((pid, sig))
            if sig == signal.SIGKILL:
                fake.kill()
            else:
                fake.send_signal(sig)

        async def noop(*args, **kwargs):
            pass  # _terminate_subprocess does nothing → proc still alive

        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-finally-kill",
            source_path="/tmp/proj",  # noqa: S108
        )
        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=track_and_drive), \
             patch("main._terminate_subprocess", side_effect=noop):
            await main._execute_claude_skill(req)

        # Finally must SIGKILL the pgroup as a backstop.
        self.assertIn((fake.pid, signal.SIGKILL), sig_calls)

    async def test_finally_does_not_double_kill_on_normal_success(self):
        """
        On the normal happy path ``_terminate_subprocess`` brings the
        proc down via SIGTERM. ``proc.returncode`` is then set, so the
        finally backstop must NOT issue a second SIGKILL — that would
        race with the next pipeline run reusing the pid.
        """
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success"}\n'],
        )
        sig_calls: list[tuple] = []

        def track_and_drive(pid, sig):
            sig_calls.append((pid, sig))
            if sig == signal.SIGKILL:
                fake.kill()
            else:
                fake.send_signal(sig)

        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-no-double-kill",
            source_path="/tmp/proj",  # noqa: S108
        )
        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=track_and_drive):
            await main._execute_claude_skill(req)

        sigterms = [(p, s) for p, s in sig_calls if s == signal.SIGTERM]
        sigkills = [(p, s) for p, s in sig_calls if s == signal.SIGKILL]
        self.assertEqual(
            len(sigterms), 1,
            f"expected exactly one SIGTERM (from _terminate_subprocess); got {sig_calls!r}",
        )
        self.assertEqual(
            len(sigkills), 0,
            f"finally must not double-kill after successful termination; got {sig_calls!r}",
        )

    async def test_except_path_returns_error_payload_without_hanging(self):
        """
        Exception inside the try block must not hang or leak: the
        finally cleanup runs unconditionally and the function returns
        a well-formed error CallbackPayload.

        Pre-refactor, the except path only cancelled jsonl_tail_task —
        five other bg_tasks survived until uvicorn shutdown. After the
        refactor, finally runs ``_cleanup_run_tasks`` for every path.
        """
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success"}\n'],
        )

        def boom(*args, **kwargs):
            msg = "forced for test"
            raise RuntimeError(msg)

        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-except-path",
            source_path="/tmp/proj",  # noqa: S108
        )
        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)), \
             patch("main._terminate_subprocess", side_effect=boom):
            payload = await asyncio.wait_for(
                main._execute_claude_skill(req), timeout=10,
            )

        self.assertEqual(payload.status, "error")
        self.assertEqual(payload.detail, "bridge internal error")


class PayloadFromResultEventTests(unittest.TestCase):

    def test_success_subtype_yields_success_payload(self):
        payload = main._payload_from_result_event(
            {"type": "result", "subtype": "success", "result": "done"},
        )
        self.assertEqual(payload.status, "success")
        self.assertEqual(payload.detail, "")

    def test_error_max_turns_preserves_subtype_and_text(self):
        payload = main._payload_from_result_event(
            {"type": "result", "subtype": "error_max_turns", "result": "hit 75 turns"},
        )
        self.assertEqual(payload.status, "error")
        self.assertIn("error_max_turns", payload.detail)
        self.assertIn("hit 75 turns", payload.detail)

    def test_error_during_execution_without_result_text(self):
        payload = main._payload_from_result_event(
            {"type": "result", "subtype": "error_during_execution"},
        )
        self.assertEqual(payload.status, "error")
        self.assertIn("error_during_execution", payload.detail)

    def test_missing_subtype_is_treated_as_error_unknown(self):
        payload = main._payload_from_result_event({"type": "result"})
        self.assertEqual(payload.status, "error")
        self.assertIn("unknown", payload.detail)

    def test_long_result_text_is_truncated(self):
        payload = main._payload_from_result_event(
            {"subtype": "error_during_execution", "result": "x" * 5000},
        )
        self.assertLess(len(payload.detail), 700)


class _KeepOpenStream:

    """Async iterator that yields lines and then blocks until ``exit_event`` fires."""

    def __init__(self, lines, exit_event):
        self._lines = list(lines)
        self._exit_event = exit_event

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._lines:
            return self._lines.pop(0)
        await self._exit_event.wait()
        raise StopAsyncIteration


class _NeverEofStream:

    """
    Stream whose drain coroutine never sees EOF — simulates orphan grandchildren
    holding the writer end of the pipe open after the CLI has been killed.
    """

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._lines:
            return self._lines.pop(0)
        await asyncio.Future()  # blocks forever until cancelled
        raise StopAsyncIteration  # pragma: no cover


class FakeProcess:

    """
    Minimal ``asyncio.subprocess.Process`` stand-in for bridge tests.

    Stdout/stderr stay open (mirroring a CLI that lingers after emitting its
    final result). ``send_signal`` optionally triggers process exit to
    simulate well-behaved vs stuck CLIs. ``stuck_pipes=True`` mimics the
    real-world failure where claude is dead but its grandchildren keep
    stdout/stderr writer ends open — used to verify the cleanup hard timeout.
    """

    def __init__(self, stdout_lines, stderr_lines=(), *,
                 exits_on_sigterm=True, stuck_pipes=False):
        self.pid = 12345
        self.returncode: int | None = None
        self.signals: list[int] = []
        self._exit_event = asyncio.Event()
        self._exits_on_sigterm = exits_on_sigterm
        if stuck_pipes:
            self.stdout = _NeverEofStream(stdout_lines)
            self.stderr = _NeverEofStream(stderr_lines)
        else:
            self.stdout = _KeepOpenStream(stdout_lines, self._exit_event)
            self.stderr = _KeepOpenStream(stderr_lines, self._exit_event)

    async def wait(self):
        await self._exit_event.wait()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGTERM and self._exits_on_sigterm:
            self.returncode = -signal.SIGTERM
            self._exit_event.set()

    def kill(self):
        self.signals.append(signal.SIGKILL)
        self.returncode = -signal.SIGKILL
        self._exit_event.set()


def _fake_signal_pgroup(fake_proc):
    """
    Build a ``_signal_pgroup`` replacement that drives the ``FakeProcess``.

    Production code now signals the whole process group, so tests must route
    those calls back into ``fake_proc.send_signal`` / ``fake_proc.kill`` to
    exercise the same exit-event machinery.
    """
    def _impl(pid, sig):
        if pid != fake_proc.pid:
            return
        if sig == signal.SIGKILL:
            fake_proc.kill()
        else:
            fake_proc.send_signal(sig)
    return _impl


class RunClaudeSkillTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._orig_timeout = main.TRIAGE_TIMEOUT
        self._orig_grace = main.POST_RESULT_GRACE
        # Keep tests fast. TRIAGE_TIMEOUT only matters for the timeout-fallback
        # test; POST_RESULT_GRACE is used whenever we terminate the CLI.
        main.TRIAGE_TIMEOUT = 2
        main.POST_RESULT_GRACE = 1

    async def asyncTearDown(self):
        main.TRIAGE_TIMEOUT = self._orig_timeout
        main.POST_RESULT_GRACE = self._orig_grace

    def _req(self):
        return AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-1",
            source_path="/tmp/proj",  # noqa: S108 — test-only path
            callback_url="",
            extra_args="",
        )

    async def _run(self, fake_proc):
        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake_proc)), \
             patch("main.httpx.AsyncClient"):
            await main._run_claude_skill(self._req())

    async def test_result_success_event_terminates_cli_with_sigterm(self):
        """Success result line must short-circuit proc.wait() and SIGTERM the CLI."""
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success","result":"ok"}\n'],
        )
        await self._run(fake)

        self.assertIn(signal.SIGTERM, fake.signals)
        self.assertNotIn(signal.SIGKILL, fake.signals)

    async def test_result_success_with_stuck_cli_falls_back_to_sigkill(self):
        """If the CLI ignores SIGTERM after the result event, we must SIGKILL."""
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success"}\n'],
            exits_on_sigterm=False,
        )
        await self._run(fake)

        self.assertIn(signal.SIGTERM, fake.signals)
        self.assertIn(signal.SIGKILL, fake.signals)

    async def test_result_error_subtype_is_propagated(self):
        fake = FakeProcess(
            stdout_lines=[
                b'{"type":"result","subtype":"error_max_turns","result":"stop"}\n',
            ],
        )
        sent_payload: dict = {}

        original = main._payload_from_result_event

        def spy(ev):
            payload = original(ev)
            sent_payload["payload"] = payload
            return payload

        with patch("main._payload_from_result_event", side_effect=spy):
            await self._run(fake)

        self.assertEqual(sent_payload["payload"].status, "error")
        self.assertIn("error_max_turns", sent_payload["payload"].detail)

    async def test_timeout_without_result_event_returns_error(self):
        """With no result event and a hung process, bridge must fall back to timeout."""
        fake = FakeProcess(
            stdout_lines=[b'{"type":"system","subtype":"init","cwd":"/x"}\n'],
            exits_on_sigterm=False,
        )
        await self._run(fake)

        # After TRIAGE_TIMEOUT (2s) elapses without a result, we should SIGTERM
        # and then SIGKILL because exits_on_sigterm=False.
        self.assertIn(signal.SIGTERM, fake.signals)
        self.assertIn(signal.SIGKILL, fake.signals)


class AnalyzeSyncEndpointTests(unittest.IsolatedAsyncioTestCase):

    """The /analyze-sync endpoint blocks until _execute_claude_skill returns."""

    async def test_returns_payload_dict_on_success(self):
        success = main.CallbackPayload(status="success")
        with patch("main._execute_claude_skill", AsyncMock(return_value=success)):
            response = await main.analyze_sync(
                AnalyzeRequest(
                    skill_name="aist-diff-security-review",
                    project_id="pipe-sync-1",
                    source_path="/tmp/proj",  # noqa: S108 — test-only path
                ),
            )
        self.assertEqual(response, {"status": "success", "detail": ""})

    async def test_returns_error_payload_when_skill_fails(self):
        failure = main.CallbackPayload(status="error", detail="claude crashed")
        with patch("main._execute_claude_skill", AsyncMock(return_value=failure)):
            response = await main.analyze_sync(
                AnalyzeRequest(
                    skill_name="aist-diff-security-review",
                    project_id="pipe-sync-2",
                    source_path="/tmp/proj",  # noqa: S108
                ),
            )
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["detail"], "claude crashed")

    async def test_400_when_required_args_missing(self):
        with self.assertRaises(HTTPException) as ctx:
            await main.analyze_sync(
                AnalyzeRequest(skill_name="", project_id="p", source_path=""),
            )
        self.assertEqual(ctx.exception.status_code, 400)


class ExecuteClaudeSkillReturnTests(unittest.IsolatedAsyncioTestCase):

    """_execute_claude_skill must return a CallbackPayload that callers can use."""

    async def asyncSetUp(self):
        self._orig_timeout = main.TRIAGE_TIMEOUT
        self._orig_grace = main.POST_RESULT_GRACE
        main.TRIAGE_TIMEOUT = 2
        main.POST_RESULT_GRACE = 1

    async def asyncTearDown(self):
        main.TRIAGE_TIMEOUT = self._orig_timeout
        main.POST_RESULT_GRACE = self._orig_grace

    async def test_returns_success_payload_on_result_success_event(self):
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success","result":"done"}\n'],
        )
        req = AnalyzeRequest(
            skill_name="aist-diff-security-review",
            project_id="pipe-exec-1",
            source_path="/tmp/proj",  # noqa: S108
        )
        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
            payload = await main._execute_claude_skill(req)
        self.assertEqual(payload.status, "success")

    async def test_bridge_log_surfaces_jsonl_while_stdout_is_buffered(self):
        """
        Regression for pipeline 05bdd13d: claude buffers stdout in batches,
        so the session jsonl is the live channel during long silent
        stretches (sub-agent activity, large tool outputs). The bridge log
        MUST surface those jsonl events independently of when stdout
        eventually flushes — previously a uuid-based dedup against
        seen_uuids could silence them, leaving operators staring at an
        empty log while jsonl grew on disk.
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="bridge-buffered-stdout-"))
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)

        log_dir = tmpdir / "logs"
        log_dir.mkdir()
        runs_dir = tmpdir / "claude-runs"
        runs_dir.mkdir()

        project_id = "pipe-buffered"
        session_id = "session-buffered"

        # Pre-write the jsonl claude would have produced silently while
        # stdout stayed buffered. _tail_jsonl reads from offset 0 on first
        # discovery, so this whole content gets picked up immediately.
        jsonl_path = (
            runs_dir / project_id / "projects" / "any" / f"{session_id}.jsonl"
        )
        jsonl_path.parent.mkdir(parents=True)
        jsonl_path.write_text(
            json.dumps({
                "type": "assistant", "uuid": "u-asst-1",
                "message": {"content": [
                    {"type": "tool_use", "name": "Bash",
                     "input": {"description": "list buffered files"}},
                ]},
            }) + "\n",
            encoding="utf-8",
        )

        # FakeProcess emits ONLY system/init on stdout — mimicking claude
        # that flushed its session header but is now silent for the rest
        # of the run. TRIAGE_TIMEOUT will trip the timeout branch and tear
        # everything down without ever seeing a result line on stdout.
        init_line = (
            json.dumps({
                "type": "system", "subtype": "init",
                "session_id": session_id, "cwd": "/x", "model": "test",
            }) + "\n"
        ).encode()
        fake = FakeProcess(stdout_lines=[init_line], exits_on_sigterm=True)

        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id=project_id,
            source_path="/tmp/proj",  # noqa: S108 — test-only path
        )

        with patch.object(main, "_CLAUDE_RUNS_DIR", runs_dir), \
             patch.object(main, "AIST_LOG_DIR", str(log_dir)), \
             patch.object(main, "_JSONL_POLL_INTERVAL", 0.1), \
             patch.object(main, "_JSONL_FIND_MAX_WAIT", 1), \
             patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
            await main._execute_claude_skill(req)

        bridge_log = log_dir / f"{project_id}.bridge.log"
        self.assertTrue(bridge_log.exists(), "bridge log file was not created")
        content = bridge_log.read_text(encoding="utf-8")
        # The assistant tool_use that lived only in the jsonl during the
        # stdout silence MUST appear in the bridge log. If it doesn't,
        # operators would be back to staring at an empty file.
        self.assertIn("Tool Bash: list buffered files", content)

    async def test_bridge_log_includes_subagent_jsonl_activity(self):
        """
        Regression for pipeline 3e565a58 (aist-full-security-review).

        Skills that delegate work via the Task tool put almost all the
        real Read/Bash/Grep/thinking inside per-subagent jsonl files at
        ``<session_id>/subagents/agent-*.jsonl``. The parent jsonl only
        records the Task ``tool_use`` and the aggregated result, so a
        watcher that only tails the parent shows nothing but
        ``Subagent started/completed`` headers — operators see hours of
        silence between those markers.

        The fix tails every file under ``<session_id>/subagents/`` in
        addition to the parent and rescans on every tick (subagents are
        spawned lazily). Each file gets a per-stem task_label so log
        lines attribute to the correct subagent.
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="bridge-subagents-"))
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)

        log_dir = tmpdir / "logs"
        log_dir.mkdir()
        runs_dir = tmpdir / "claude-runs"
        runs_dir.mkdir()

        project_id = "pipe-subagents"
        session_id = "session-with-agents"

        # Real claude layout:
        #   <runs>/<pid>/projects/<derived>/<sid>.jsonl                parent
        #   <runs>/<pid>/projects/<derived>/<sid>/subagents/agent-*.jsonl
        project_dir = runs_dir / project_id / "projects" / "any"
        project_dir.mkdir(parents=True)
        parent_jsonl = project_dir / f"{session_id}.jsonl"
        parent_jsonl.write_text(
            json.dumps({
                "type": "assistant", "uuid": "u-parent",
                "message": {"content": [{
                    "type": "tool_use", "name": "Task",
                    "input": {"description": "Explore auth", "skill": "aist-explore"},
                }]},
            }) + "\n",
            encoding="utf-8",
        )

        subagents_dir = project_dir / session_id / "subagents"
        subagents_dir.mkdir(parents=True)
        agent_jsonl = subagents_dir / "agent-deadbeef1234.jsonl"
        agent_jsonl.write_text(
            json.dumps({
                "type": "assistant", "uuid": "u-agent",
                "message": {"content": [{
                    "type": "tool_use", "name": "Bash",
                    "input": {"description": "list auth files"},
                }]},
            }) + "\n",
            encoding="utf-8",
        )

        init_line = (
            json.dumps({
                "type": "system", "subtype": "init",
                "session_id": session_id, "cwd": "/x", "model": "test",
            }) + "\n"
        ).encode()
        fake = FakeProcess(stdout_lines=[init_line], exits_on_sigterm=True)

        req = AnalyzeRequest(
            skill_name="aist-full-security-review",
            project_id=project_id,
            source_path="/tmp/proj",  # noqa: S108 — test-only path
        )

        with patch.object(main, "_CLAUDE_RUNS_DIR", runs_dir), \
             patch.object(main, "AIST_LOG_DIR", str(log_dir)), \
             patch.object(main, "_JSONL_POLL_INTERVAL", 0.1), \
             patch.object(main, "_JSONL_FIND_MAX_WAIT", 1), \
             patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
            await main._execute_claude_skill(req)

        bridge_log = log_dir / f"{project_id}.bridge.log"
        self.assertTrue(bridge_log.exists())
        content = bridge_log.read_text(encoding="utf-8")
        # Parent's Task tool_use logged from the parent jsonl.
        self.assertIn("Tool Task: Explore auth", content)
        # The whole point of this test: subagent activity must surface,
        # not just the "Subagent started/completed" markers from stdout.
        self.assertIn("Tool Bash: list auth files", content)
        # And it must carry the agent-id so operators can attribute it.
        self.assertIn("agent-deadbeef1234", content)

    async def test_partial_jsonl_line_is_not_lost(self):
        r"""
        POSIX write(2) on regular files is not atomic for ``len > 1``,
        so a poll landing between the two halves of a long claude write
        sees a line without its closing ``\n``. The tailer must hold
        those partial bytes back instead of advancing past them: if the
        offset crosses the middle of the line, ``json.loads`` will fail
        on both halves on subsequent ticks and the event vanishes.

        Reproduced by writing a bare prefix, polling once (the tail
        should refuse to consume), then appending the suffix, polling
        again (the now-complete event must surface — exactly once).
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="bridge-partial-write-"))
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)

        log_dir = tmpdir / "logs"
        log_dir.mkdir()
        runs_dir = tmpdir / "claude-runs"
        runs_dir.mkdir()

        project_id = "pipe-partial"
        session_id = "sess-partial"

        project_dir = runs_dir / project_id / "projects" / "any"
        project_dir.mkdir(parents=True)
        parent_jsonl = project_dir / f"{session_id}.jsonl"

        full_event = json.dumps({
            "type": "assistant", "uuid": "u-partial",
            "message": {"content": [{
                "type": "tool_use", "name": "Bash",
                "input": {"description": "huge command captured mid-write"},
            }]},
        })
        # Split well before the closing brace so the prefix alone fails
        # json.loads — guarantees that "lazy commit" is the only way the
        # event ever surfaces.
        split = len(full_event) // 2
        prefix, suffix = full_event[:split].encode(), full_event[split:].encode() + b"\n"

        # Write the prefix BEFORE the bridge starts so the first poll
        # sees a partial line. The suffix is appended in-flight.
        parent_jsonl.write_bytes(prefix)

        async def _append_suffix_after_delay():
            await asyncio.sleep(0.4)
            with parent_jsonl.open("ab") as f:
                f.write(suffix)

        init_line = (
            json.dumps({
                "type": "system", "subtype": "init",
                "session_id": session_id, "cwd": "/x", "model": "m",
            }) + "\n"
        ).encode()
        fake = FakeProcess(stdout_lines=[init_line], exits_on_sigterm=True)

        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id=project_id,
            source_path="/tmp/proj",  # noqa: S108
        )

        prev_timeout = main.TRIAGE_TIMEOUT
        main.TRIAGE_TIMEOUT = 3
        try:
            with patch.object(main, "_CLAUDE_RUNS_DIR", runs_dir), \
                 patch.object(main, "AIST_LOG_DIR", str(log_dir)), \
                 patch.object(main, "_JSONL_POLL_INTERVAL", 0.1), \
                 patch.object(main, "_JSONL_FIND_MAX_WAIT", 1), \
                 patch("main._build_skill_prompt", return_value="prompt"), \
                 patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
                 patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
                appender = asyncio.create_task(_append_suffix_after_delay())
                try:
                    await main._execute_claude_skill(req)
                finally:
                    if not appender.done():
                        appender.cancel()
        finally:
            main.TRIAGE_TIMEOUT = prev_timeout

        content = (log_dir / f"{project_id}.bridge.log").read_text(encoding="utf-8")
        # Event must surface in full, exactly once — not split, not lost.
        tool_lines = [ln for ln in content.splitlines() if "huge command captured mid-write" in ln]
        self.assertEqual(
            len(tool_lines), 1,
            f"event must be logged exactly once after the suffix arrives; got: {tool_lines!r}",
        )

    async def test_subagent_jsonl_created_after_tail_starts_is_picked_up(self):
        """
        Subagents are spawned lazily, often minutes into a session. The
        rescan must pick up files that appear AFTER the tailer has
        already started watching — not only what existed at first poll.
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="bridge-subagents-late-"))
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)

        log_dir = tmpdir / "logs"
        log_dir.mkdir()
        runs_dir = tmpdir / "claude-runs"
        runs_dir.mkdir()

        project_id = "pipe-late-agent"
        session_id = "sess-late"

        project_dir = runs_dir / project_id / "projects" / "any"
        project_dir.mkdir(parents=True)
        parent_jsonl = project_dir / f"{session_id}.jsonl"
        parent_jsonl.write_text("", encoding="utf-8")  # empty parent
        subagents_dir = project_dir / session_id / "subagents"
        subagents_dir.mkdir(parents=True)

        init_line = (
            json.dumps({
                "type": "system", "subtype": "init",
                "session_id": session_id, "cwd": "/x", "model": "m",
            }) + "\n"
        ).encode()
        fake = FakeProcess(stdout_lines=[init_line], exits_on_sigterm=True)

        async def _spawn_subagent_after_delay():
            # Give _tail_jsonl time to find the parent and enter its loop,
            # then drop a subagent jsonl. The next rescan tick must pick it up.
            await asyncio.sleep(0.4)
            (subagents_dir / "agent-late9876.jsonl").write_text(
                json.dumps({
                    "type": "assistant", "uuid": "u-late",
                    "message": {"content": [{
                        "type": "tool_use", "name": "Read",
                        "input": {"file_path": "/etc/late.conf"},
                    }]},
                }) + "\n",
                encoding="utf-8",
            )

        req = AnalyzeRequest(
            skill_name="aist-full-security-review",
            project_id=project_id,
            source_path="/tmp/proj",  # noqa: S108
        )

        # TRIAGE_TIMEOUT raised slightly so the late-write + next rescan
        # tick fits into the run window.
        prev_timeout = main.TRIAGE_TIMEOUT
        main.TRIAGE_TIMEOUT = 3
        try:
            with patch.object(main, "_CLAUDE_RUNS_DIR", runs_dir), \
                 patch.object(main, "AIST_LOG_DIR", str(log_dir)), \
                 patch.object(main, "_JSONL_POLL_INTERVAL", 0.1), \
                 patch.object(main, "_JSONL_FIND_MAX_WAIT", 1), \
                 patch("main._build_skill_prompt", return_value="prompt"), \
                 patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
                 patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
                spawner = asyncio.create_task(_spawn_subagent_after_delay())
                try:
                    await main._execute_claude_skill(req)
                finally:
                    if not spawner.done():
                        spawner.cancel()
        finally:
            main.TRIAGE_TIMEOUT = prev_timeout

        content = (log_dir / f"{project_id}.bridge.log").read_text(encoding="utf-8")
        self.assertIn("Tool Read: /etc/late.conf", content)
        self.assertIn("agent-late9876", content)

    async def test_stdout_logs_only_stream_json_only_event_types(self):
        """
        Variant B contract: assistant/user content comes from jsonl.
        ``_stream_stdout`` only forwards event types that don't appear
        in jsonl at all (system, result, rate_limit_event), so the
        operator still gets the session header / API-retry warnings /
        final result summary. assistant/user turns are dropped here —
        otherwise they'd race the jsonl tail and double-log.
        """
        fake = FakeProcess(
            stdout_lines=[
                # Initial session header — must be logged (system).
                b'{"type":"system","subtype":"init","session_id":"s1","cwd":"/x","model":"m"}\n',
                # API-retry warning — must be logged (system).
                b'{"type":"system","subtype":"api_retry","attempt":1,"max_retries":5,"retry_delay_ms":250,"error":"x","error_status":500}\n',
                # Assistant turn — MUST NOT be logged here (jsonl owns it).
                b'{"type":"assistant","uuid":"u1","message":{"content":[{"type":"text","text":"from-stdout"}]}}\n',
                # Tool result on user turn — MUST NOT be logged here either.
                b'{"type":"user","uuid":"u2","message":{"content":[{"type":"tool_result","tool_use_id":"t","content":"x"}]}}\n',
                # Result summary — must be logged (result).
                b'{"type":"result","subtype":"success","duration_ms":100,"num_turns":1,"result":"done"}\n',
            ],
        )
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-stdout-whitelist",
            source_path="/tmp/proj",  # noqa: S108
        )
        log_calls: list[dict] = []
        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)), \
             patch("main._log_claude_event",
                   side_effect=lambda _log, _label, ev: log_calls.append(ev)):
            payload = await main._execute_claude_skill(req)

        self.assertEqual(payload.status, "success")
        logged_types = [ev.get("type") for ev in log_calls]
        self.assertNotIn("assistant", logged_types,
                         "assistant turns must come from jsonl, not stdout")
        self.assertNotIn("user", logged_types,
                         "user turns must come from jsonl, not stdout")
        self.assertIn("system", logged_types,
                      "system events are stream-json-only and must be logged")
        self.assertIn("result", logged_types,
                      "result summary is stream-json-only and must be logged")

    async def test_returns_when_grandchildren_keep_pipes_open_after_kill(self):
        """
        Regression for pipeline a2a7ed26: the claude CLI was killed but
        sub-agent grandchildren held stdout/stderr writers open, so the
        drain coroutines never saw EOF and ``_execute_claude_skill`` hung
        forever — leaving the celery worker blocked on /analyze-sync for
        ~3h until httpx ReadTimeout.

        The cleanup must abandon a stream that refuses to EOF and return
        a payload anyway, so the HTTP response is not held hostage by an
        orphan process tree.
        """
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success","result":"ok"}\n'],
            stuck_pipes=True,        # streams never EOF, mimicking orphan grandchildren
            exits_on_sigterm=True,   # CLI itself dies normally, only pipes linger
        )
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-stuck-1",
            source_path="/tmp/proj",  # noqa: S108
        )
        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
            # Whole flow must finish well within the cleanup ceiling
            # (POST_RESULT_GRACE=1s + 5s drain timeout per stream).
            payload = await asyncio.wait_for(
                main._execute_claude_skill(req), timeout=20,
            )
        self.assertEqual(payload.status, "success")


class OpenPipelineLogHandlerTests(unittest.TestCase):

    """
    Bridge logs must land in ``<pid>.bridge.log``, not the main pipeline log.

    On Linux the main log file is owned by root (celeryworker writes it as
    ``user: 0:0``) and the bridge runs as ``claude``. Appending to a
    root-owned file would fail and the bridge would silently lose its logs.
    Writing to a separate file keeps the bridge as the sole writer.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="bridge-log-test-")
        self._orig_log_dir = main.AIST_LOG_DIR
        main.AIST_LOG_DIR = self._tmpdir

    def tearDown(self):
        main.AIST_LOG_DIR = self._orig_log_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_handler_writes_to_bridge_suffixed_file(self):
        handler = main._open_pipeline_log_handler("pipeline-abc")
        self.assertIsNotNone(handler)
        try:
            self.assertEqual(
                Path(handler.baseFilename).name,
                "pipeline-abc.bridge.log",
            )
            self.assertEqual(
                Path(handler.baseFilename).parent,
                Path(self._tmpdir),
            )
        finally:
            handler.close()

    def test_handler_does_not_collide_with_main_pipeline_log(self):
        # If the celeryworker has already created the main log, the bridge
        # must still be able to open its own file (different filename → no
        # ownership collision on Linux).
        (Path(self._tmpdir) / "pipeline-xyz.log").write_text("root-owned-line\n", encoding="utf-8")
        handler = main._open_pipeline_log_handler("pipeline-xyz")
        self.assertIsNotNone(handler)
        try:
            self.assertNotEqual(
                Path(handler.baseFilename).name,
                "pipeline-xyz.log",
                "bridge must NOT open the main pipeline log file",
            )
            self.assertTrue(Path(handler.baseFilename).name.endswith(".bridge.log"))
        finally:
            handler.close()

    def test_handler_returns_none_when_log_dir_unset(self):
        main.AIST_LOG_DIR = ""
        try:
            self.assertIsNone(main._open_pipeline_log_handler("any"))
        finally:
            main.AIST_LOG_DIR = self._tmpdir


if __name__ == "__main__":
    unittest.main()
