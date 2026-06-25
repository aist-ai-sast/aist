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
from pydantic import ValidationError

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


class _EofAfterLinesStream:

    """
    Async iterator that yields the given lines and then EOFs immediately.

    Used to model the asyncio child-watcher race: claude has exited and
    been reaped (by tini), the pipe is in EOF state, but the asyncio
    transport's proc.wait() is wedged because the SIGCHLD never reached
    asyncio. See ``_HangingFakeProcess``.
    """

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._lines:
            return self._lines.pop(0)
        raise StopAsyncIteration


class _HangingFakeProcess:

    """
    Stdout/stderr pipes EOF, but ``proc.wait()`` never returns.

    Mirrors the post-mortem of pipeline 4a1912d7: claude actually
    exited, was reaped by tini, the pipes are closed, but asyncio's
    ``ThreadedChildWatcher`` lost the SIGCHLD race and ``proc.wait()``
    is wedged. Bridge must wake on stdout EOF and synthesise a result.
    """

    def __init__(self, stdout_lines, stderr_lines=()):
        self.pid = 54321
        self.returncode = None  # never set — that is the whole point
        self.signals: list[int] = []
        self.stdout = _EofAfterLinesStream(stdout_lines)
        self.stderr = _EofAfterLinesStream(stderr_lines)

    async def wait(self):
        # Hang forever — simulates the asyncio child-watcher race.
        # Cancellation is the only way out, which is exactly how the
        # bridge's finally block recovers in production.
        await asyncio.Future()

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.signals.append(signal.SIGKILL)


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


class BuildClaudeCmdTests(unittest.TestCase):

    """``--model`` plumbing: per-request model, env default, and validation."""

    def test_no_model_omits_model_flag(self):
        cmd = main._build_claude_cmd("PROMPT", "")
        self.assertNotIn("--model", cmd)
        # Sanity: the core flags are still present and prompt is positional.
        self.assertEqual(cmd[:3], [main.CLAUDE_PATH, "-p", "PROMPT"])
        self.assertIn("--output-format", cmd)

    def test_model_is_inserted_after_prompt(self):
        cmd = main._build_claude_cmd("PROMPT", "opus")
        self.assertEqual(cmd[:5], [main.CLAUDE_PATH, "-p", "PROMPT", "--model", "opus"])
        # Model value never leaks into another flag position.
        self.assertEqual(cmd.count("--model"), 1)

    def test_full_model_id_is_passed_through(self):
        cmd = main._build_claude_cmd("PROMPT", "claude-opus-4-8")
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-4-8")

    def test_request_model_overrides_bridge_default(self):
        with patch.object(main, "CLAUDE_BRIDGE_MODEL", "sonnet"):
            self.assertEqual(main._resolve_model("opus"), "opus")

    def test_bridge_default_used_when_request_model_empty(self):
        with patch.object(main, "CLAUDE_BRIDGE_MODEL", "sonnet"):
            self.assertEqual(main._resolve_model(""), "sonnet")

    def test_empty_when_neither_set(self):
        with patch.object(main, "CLAUDE_BRIDGE_MODEL", ""):
            self.assertEqual(main._resolve_model(""), "")

    def test_analyze_request_accepts_valid_model(self):
        # Aliases, full ids, and the documented 1M-context bracket variants.
        for good in ("opus", "sonnet", "haiku", "fable", "opus[1m]",
                     "claude-opus-4-8", "claude-opus-4-8[1m]",
                     "claude-haiku-4-5-20251001"):
            req = AnalyzeRequest(
                skill_name="aist-finding-triage",
                project_id="p",
                source_path="/tmp/proj",  # noqa: S108 — test-only path
                model=good,
            )
            self.assertEqual(req.model, good)

    def test_analyze_request_model_defaults_to_empty(self):
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="p",
            source_path="/tmp/proj",  # noqa: S108 — test-only path
        )
        self.assertEqual(req.model, "")

    def test_analyze_request_rejects_flag_shaped_model(self):
        # A leading dash must be rejected so the value can never be parsed as
        # an extra CLI flag when spliced into the claude argv.
        for bad in ("--dangerously-skip-permissions", "-p", "a b", "x/y", "a;b"):
            with self.assertRaises(ValidationError):
                AnalyzeRequest(
                    skill_name="aist-finding-triage",
                    project_id="p",
                    source_path="/tmp/proj",  # noqa: S108 — test-only path
                    model=bad,
                )


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


class AnalyzeRequestSubprocessEnvTests(unittest.TestCase):

    """
    Task 4: AnalyzeRequest carries a per-run subprocess_env dict whose
    values are pydantic ``SecretStr`` — masked in repr/log by default.

    The field is generic (any agent-bridge user can pass arbitrary env
    vars), keeping the bridge agent-agnostic at the type level.
    """

    def test_default_is_empty_dict(self):
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="p1",
            source_path="/tmp/x",  # noqa: S108 -- test path
        )
        self.assertEqual(dict(req.subprocess_env), {})

    def test_accepts_string_values_and_wraps_in_secret_str(self):
        from pydantic import SecretStr  # noqa: PLC0415

        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="p1",
            source_path="/tmp/x",  # noqa: S108
            subprocess_env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret-value-xx"},
        )
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", req.subprocess_env)
        self.assertIsInstance(req.subprocess_env["CLAUDE_CODE_OAUTH_TOKEN"], SecretStr)
        self.assertEqual(
            req.subprocess_env["CLAUDE_CODE_OAUTH_TOKEN"].get_secret_value(),
            "sk-ant-oat01-secret-value-xx",
        )

    def test_model_dump_masks_secret_values(self):
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="p1",
            source_path="/tmp/x",  # noqa: S108
            subprocess_env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-super-secret"},
        )
        dumped = req.model_dump()
        # SecretStr default str() / model_dump masks. The literal secret
        # MUST NOT appear in the dump output — defence-in-depth in case
        # someone logs req.model_dump() during debugging.
        self.assertNotIn("sk-ant-oat01-super-secret", str(dumped))


class RedactingFilterTests(unittest.TestCase):

    """
    Task 4: a logging.Filter installed per-run masks any secret value
    that leaks into a log line (e.g. claude CLI echoing the token in an
    auth-error message). Agent-agnostic — works for any secret list
    passed at construction time.
    """

    def _make_record(self, msg, args=()):
        return logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=0,
            msg=msg, args=args, exc_info=None,
        )

    def test_filter_with_no_secrets_passes_records_through(self):
        f = main._RedactingFilter([])
        rec = self._make_record("hello world")
        self.assertTrue(f.filter(rec))
        self.assertEqual(rec.getMessage(), "hello world")

    def test_filter_redacts_known_secret(self):
        f = main._RedactingFilter(["sk-ant-oat01-super-secret"])
        rec = self._make_record("auth fail: oat_super_secret invalid")
        self.assertTrue(f.filter(rec))
        rendered = rec.getMessage()
        self.assertNotIn("sk-ant-oat01-super-secret", rendered)
        self.assertIn("***REDACTED***", rendered)

    def test_filter_handles_args_substitution(self):
        # Log calls typically use format args: ``log.info("[%s] %s", label, text)``.
        # Filter must see the fully-formatted message, not the unformatted
        # template — otherwise secrets inside ``%s`` arguments slip through.
        f = main._RedactingFilter(["sk-ant-oat01-secret"])
        rec = self._make_record("[%s] stderr: %s", args=("label", "saw sk-ant-oat01-secret"))
        self.assertTrue(f.filter(rec))
        self.assertNotIn("sk-ant-oat01-secret", rec.getMessage())

    def test_filter_ignores_empty_secrets(self):
        # Empty-string in the secret list would otherwise make
        # str.replace("", "X") insert REDACTED between every char.
        f = main._RedactingFilter(["", "oat_real"])
        rec = self._make_record("hello oat_real")
        self.assertTrue(f.filter(rec))
        self.assertEqual(rec.getMessage(), "hello ***REDACTED***")


class ExecuteClaudeSkillInjectsSubprocessEnvTests(unittest.IsolatedAsyncioTestCase):

    """
    Task 4 e2e: ``_execute_claude_skill`` merges ``req.subprocess_env``
    into the spawn env passed to ``asyncio.create_subprocess_exec``, and
    installs the redacting filter on the per-run logger.

    No interaction with ``os.environ`` beyond inheriting the existing
    bridge container env (which by Task 9 must NOT contain Claude
    credentials).
    """

    async def asyncSetUp(self):
        self._orig_timeout = main.TRIAGE_TIMEOUT
        self._orig_grace = main.POST_RESULT_GRACE
        main.TRIAGE_TIMEOUT = 2
        main.POST_RESULT_GRACE = 1

    async def asyncTearDown(self):
        main.TRIAGE_TIMEOUT = self._orig_timeout
        main.POST_RESULT_GRACE = self._orig_grace

    async def test_subprocess_env_token_passed_to_create_subprocess_exec(self):
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success","result":"ok"}\n'],
        )
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-task4",
            source_path="/tmp/p",  # noqa: S108
            subprocess_env={"CLAUDE_CODE_OAUTH_TOKEN": "oat_token_value_abc"},
        )

        exec_mock = AsyncMock(return_value=fake)
        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", exec_mock), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
            await asyncio.wait_for(main._execute_claude_skill(req), timeout=20)

        # Inspect kwargs of the single create_subprocess_exec call —
        # spawn env must contain the per-request token.
        _, kwargs = exec_mock.call_args
        env = kwargs.get("env") or {}
        self.assertEqual(env.get("CLAUDE_CODE_OAUTH_TOKEN"), "oat_token_value_abc")

    async def test_subprocess_env_secret_never_logged(self):
        """
        Even if claude echoes the token in stderr (common on auth-401),
        the bridge log must not contain the raw secret.
        """
        secret = "oat_should_not_appear_in_log"  # noqa: S105
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success","result":"ok"}\n'],
            stderr_lines=[f"401 invalid token: {secret}".encode()],
        )
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-task4-redact",
            source_path="/tmp/p",  # noqa: S108
            subprocess_env={"CLAUDE_CODE_OAUTH_TOKEN": secret},
        )

        captured_logs: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, rec):
                captured_logs.append(rec.getMessage())

        cap = _Capture()
        target_logger = logging.getLogger(f"aist-triage-bridge.task.{req.project_id}")
        target_logger.addHandler(cap)
        try:
            with patch("main._build_skill_prompt", return_value="prompt"), \
                 patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
                 patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
                await asyncio.wait_for(main._execute_claude_skill(req), timeout=20)
        finally:
            target_logger.removeHandler(cap)

        # The stderr line containing the secret MUST have been captured
        # (otherwise the test is no-op) — and the captured form must be
        # redacted.
        stderr_lines = [line for line in captured_logs if "stderr" in line]
        self.assertTrue(stderr_lines, "expected at least one stderr log line")
        for line in captured_logs:
            self.assertNotIn(secret, line, f"Secret leaked into log line: {line!r}")


class SecretRedactionFileHandlerE2ETests(unittest.IsolatedAsyncioTestCase):

    """
    Task 12 — confirm the redaction installed in Task 4 survives the
    full pipeline log path (RotatingFileHandler attached during
    ``_execute_claude_skill``). A regression here would mean operators
    read a token off the on-disk pipeline log even though the in-memory
    log capture is clean.
    """

    async def asyncSetUp(self):
        self._orig_timeout = main.TRIAGE_TIMEOUT
        self._orig_grace = main.POST_RESULT_GRACE
        self._orig_log_dir = main.AIST_LOG_DIR
        main.TRIAGE_TIMEOUT = 2
        main.POST_RESULT_GRACE = 1
        self._tmpdir = tempfile.mkdtemp(prefix="bridge-redact-e2e-")
        main.AIST_LOG_DIR = self._tmpdir

    async def asyncTearDown(self):
        main.TRIAGE_TIMEOUT = self._orig_timeout
        main.POST_RESULT_GRACE = self._orig_grace
        main.AIST_LOG_DIR = self._orig_log_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    async def test_pipeline_log_file_redacts_token_echoed_in_stderr(self):
        secret = "oat_token_must_be_redacted_on_disk"  # noqa: S105
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success","result":"ok"}\n'],
            # Stderr line that mimics claude's auth-failure echo (real
            # CLI does this for some error paths; see plan Task 4 / 12).
            stderr_lines=[f"401 auth failed; token={secret}".encode()],
        )
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-redact-e2e",
            source_path="/tmp/p",  # noqa: S108
            subprocess_env={"CLAUDE_CODE_OAUTH_TOKEN": secret},
        )

        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
            await asyncio.wait_for(main._execute_claude_skill(req), timeout=20)

        log_file = Path(self._tmpdir) / "pipe-redact-e2e.bridge.log"
        self.assertTrue(log_file.exists(), "bridge log file must be created")
        content = log_file.read_text(encoding="utf-8")
        self.assertNotIn(secret, content)
        # The stderr line that originally contained the secret must be
        # present in the on-disk log in its redacted form. Otherwise the
        # filter is silently dropping the line which loses diagnostic
        # value for operators.
        self.assertIn("***REDACTED***", content)
        self.assertIn("401 auth failed", content)


class StdoutEofFallbackTests(unittest.IsolatedAsyncioTestCase):

    """
    Defence-in-depth completion path for the asyncio ThreadedChildWatcher
    ↔ tini race (incident pipeline 4a1912d7).

    In production: claude finished its work, exited, tini reaped it
    before asyncio could observe the SIGCHLD. Pipe is in EOF, but
    ``proc.wait()`` hangs forever. Bridge must wake on stdout EOF and
    synthesise success/error based on whether claude ever produced any
    assistant output.
    """

    async def asyncSetUp(self):
        self._orig_timeout = main.TRIAGE_TIMEOUT
        self._orig_grace = main.POST_RESULT_GRACE
        # Short ceiling — the EOF fallback must fire well within this.
        # If the test ever sits here for the full TRIAGE_TIMEOUT,
        # something in the wait set is broken.
        main.TRIAGE_TIMEOUT = 5
        main.POST_RESULT_GRACE = 1

    async def asyncTearDown(self):
        main.TRIAGE_TIMEOUT = self._orig_timeout
        main.POST_RESULT_GRACE = self._orig_grace

    async def test_stdout_eof_with_assistant_turn_synthesizes_success(self):
        fake = _HangingFakeProcess(stdout_lines=[
            b'{"type":"system","subtype":"init","cwd":"/x","session_id":"abc","model":"opus"}\n',
            b'{"type":"assistant","message":{"content":[{"type":"text","text":"All findings triaged."}]}}\n',
        ])
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-eof-success",
            source_path="/tmp/p",  # noqa: S108
        )

        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup"):
            payload = await asyncio.wait_for(main._execute_claude_skill(req), timeout=20)

        self.assertEqual(payload.status, "success")

    async def test_stdout_eof_without_assistant_turn_returns_error(self):
        # Crash-in-init scenario: session/init line was emitted but no
        # assistant turn ever produced. Bridge must NOT synthesise a
        # false success — the pipeline should be marked degraded.
        fake = _HangingFakeProcess(stdout_lines=[
            b'{"type":"system","subtype":"init","cwd":"/x","session_id":"abc","model":"opus"}\n',
        ])
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-eof-error",
            source_path="/tmp/p",  # noqa: S108
        )

        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup"):
            payload = await asyncio.wait_for(main._execute_claude_skill(req), timeout=20)

        self.assertEqual(payload.status, "error")
        self.assertIn("without producing", payload.detail)

    async def test_result_event_wins_over_stdout_eof(self):
        # When claude DOES emit a clean result event, that takes priority
        # over the EOF fallback even if both fire — the success/error
        # subtype on the result event carries more information than the
        # had_assistant_turn heuristic.
        fake = _HangingFakeProcess(stdout_lines=[
            b'{"type":"system","subtype":"init","cwd":"/x","session_id":"abc","model":"opus"}\n',
            b'{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}\n',
            b'{"type":"result","subtype":"success","result":"ok"}\n',
        ])
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="pipe-eof-race",
            source_path="/tmp/p",  # noqa: S108
        )

        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup"):
            payload = await asyncio.wait_for(main._execute_claude_skill(req), timeout=20)

        # subtype=success → success, no fallback prefix in detail.
        self.assertEqual(payload.status, "success")


def _make_run_ctx():
    """Bare ``_RunContext`` for direct ``_stream_stdout`` unit tests."""
    return main._RunContext(
        log=logging.getLogger("aist-triage-bridge.test.stream"),
        task_label="test",
        claude_config_dir=Path("/tmp/bridge-test-ctx"),  # noqa: S108 — unused on this path
        result_file_path=None,
    )


def _stream_json_line(size_bytes: int) -> bytes:
    """
    A single stream-json ``user`` event carrying a tool_result whose payload is
    at least ``size_bytes`` long — models claude echoing a large tool_result
    (all findings / a big diff) on one line. Returns the newline-terminated
    bytes the StreamReader will see.
    """
    big = "x" * size_bytes
    event = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": big}]},
    }
    line = (json.dumps(event) + "\n").encode("utf-8")
    assert len(line) > size_bytes  # noqa: S101 — guards the >64 KiB premise
    return line


class StreamStdoutLargeLineTests(unittest.IsolatedAsyncioTestCase):

    """
    Regression for incident pipeline f4dbe4aa: a stream-json line larger than
    asyncio's default 64 KiB ``StreamReader`` limit (a tool_result echo with
    all findings) crashed ``_stream_stdout`` mid-stream, which was then misread
    as a clean EOF + false ``success``.

    These drive ``_stream_stdout`` against a REAL ``asyncio.StreamReader`` so
    the buffer-limit behaviour is exercised, not mocked away.
    """

    # 200 KiB — comfortably over the old 64 KiB default, well under the new ceiling.
    _BIG = 200 * 1024

    async def test_large_line_streams_intact_and_result_event_parsed(self):
        # With the raised limit, the oversized tool_result line is read whole,
        # the following result event is parsed, and the reader does NOT fail.
        reader = asyncio.StreamReader(limit=main._STREAM_READER_LIMIT)
        reader.feed_data(_stream_json_line(self._BIG))
        reader.feed_data(b'{"type":"result","subtype":"success","result":"done"}\n')
        reader.feed_eof()

        ctx = _make_run_ctx()
        await main._stream_stdout(reader, ctx)

        self.assertFalse(ctx.stdout_reader_failed)
        self.assertTrue(ctx.stdout_done.is_set())
        self.assertIsNotNone(ctx.payload)
        self.assertEqual(ctx.payload.status, "success")

    async def test_oversized_line_is_dropped_and_stream_continues(self):
        # Per-line resilience: at a limit the big line overruns (old 64 KiB
        # default), ``_stream_stdout`` must DROP that one line and keep reading
        # so the small ``result`` event that follows is still parsed. This is
        # the real incident shape: a giant tool_result echo followed by the
        # genuine result event. The run is NOT a reader failure.
        reader = asyncio.StreamReader(limit=64 * 1024)
        reader.feed_data(_stream_json_line(self._BIG))
        reader.feed_data(b'{"type":"result","subtype":"success","result":"done"}\n')
        reader.feed_eof()

        ctx = _make_run_ctx()
        await main._stream_stdout(reader, ctx)

        self.assertFalse(ctx.stdout_reader_failed)
        self.assertGreaterEqual(ctx.stdout_lines_dropped, 1)
        self.assertTrue(ctx.stdout_done.is_set())
        self.assertIsNotNone(ctx.payload)
        self.assertEqual(ctx.payload.status, "success")

    async def test_unexpected_reader_error_flags_failure(self):
        # An UNEXPECTED reader error (not a line overrun) must NOT be laundered
        # into a clean EOF: it sets ``stdout_reader_failed`` so the caller can
        # report error rather than synthesise success.
        ctx = _make_run_ctx()
        await main._stream_stdout(_RaisingStream(RuntimeError("boom")), ctx)

        self.assertTrue(ctx.stdout_reader_failed)
        self.assertTrue(ctx.stdout_done.is_set())
        self.assertIsNone(ctx.payload)

    async def test_subprocess_is_launched_with_raised_limit(self):
        # The fix only helps if the limit actually reaches the real subprocess.
        fake = FakeProcess(
            stdout_lines=[b'{"type":"result","subtype":"success","result":"ok"}\n'],
        )
        req = AnalyzeRequest(
            skill_name="aist-diff-security-review",
            project_id="pipe-limit",
            source_path="/tmp/proj",  # noqa: S108
        )
        spy = AsyncMock(return_value=fake)
        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", spy), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
            await main._execute_claude_skill(req)

        self.assertEqual(spy.call_args.kwargs["limit"], main._STREAM_READER_LIMIT)


class ValidResultFilePayloadTests(unittest.TestCase):

    """
    ``_valid_result_file_payload`` is the authoritative-artifact check used to
    recover a run whose stdout reader crashed. Only a present, non-empty, valid
    JSON file counts as success; everything else is ``None`` (→ caller errors).
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="bridge-valid-result-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _ctx(self, path):
        ctx = _make_run_ctx()
        ctx.result_file_path = path
        return ctx

    def test_none_path_returns_none(self):
        self.assertIsNone(main._valid_result_file_payload(self._ctx(None)))

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            main._valid_result_file_payload(self._ctx(self.tmpdir / "absent.json")),
        )

    def test_empty_file_returns_none(self):
        path = self.tmpdir / "empty.json"
        path.write_text("   \n", encoding="utf-8")
        self.assertIsNone(main._valid_result_file_payload(self._ctx(path)))

    def test_malformed_json_returns_none(self):
        path = self.tmpdir / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(main._valid_result_file_payload(self._ctx(path)))

    def test_valid_nonempty_json_returns_success(self):
        path = self.tmpdir / "ok.json"
        path.write_text(json.dumps({"findings": []}), encoding="utf-8")
        payload = main._valid_result_file_payload(self._ctx(path))
        self.assertIsNotNone(payload)
        self.assertEqual(payload.status, "success")


class _RaisingStream:

    """
    Stdout iterator that yields its lines, then raises ``exc`` on the next read.

    Models an UNEXPECTED reader failure (not a recoverable line overrun) at the
    ``__anext__`` boundary — the residual ``stdout_reader_failed`` path the
    bridge must still treat as an error rather than a clean EOF.
    """

    def __init__(self, exc, lines=()):
        self._exc = exc
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._lines:
            return self._lines.pop(0)
        raise self._exc


class _ReaderCrashProcess:

    """
    ``proc.wait()`` hangs (claude still alive) while the stdout reader crashes
    with an unexpected error.

    This is the dangerous shape: ``stdout_done`` fires from the reader's
    ``finally`` even though the process never exited. The bridge must report
    error (or recover from a valid result file), never synthesise a bare
    success.
    """

    def __init__(self, stdout_lines, stderr_lines=()):
        self.pid = 67890
        self.returncode = None
        self.signals: list[int] = []
        self.stdout = _RaisingStream(RuntimeError("stdout reader boom"), stdout_lines)
        self.stderr = _EofAfterLinesStream(stderr_lines)

    async def wait(self):
        await asyncio.Future()  # hang until cancelled

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.signals.append(signal.SIGKILL)


class StdoutReaderCrashTests(unittest.IsolatedAsyncioTestCase):

    """
    The reader crash must NOT be laundered into ``success`` by the stdout-EOF
    fallback (incident pipeline f4dbe4aa). With no result event and a crashed
    reader, ``_execute_claude_skill`` must return ``error``.
    """

    async def asyncSetUp(self):
        self._orig_timeout = main.TRIAGE_TIMEOUT
        self._orig_grace = main.POST_RESULT_GRACE
        main.TRIAGE_TIMEOUT = 5
        main.POST_RESULT_GRACE = 1

    async def asyncTearDown(self):
        main.TRIAGE_TIMEOUT = self._orig_timeout
        main.POST_RESULT_GRACE = self._orig_grace

    async def test_reader_crash_without_result_event_returns_error(self):
        # An assistant turn was emitted before the crash, so the old EOF
        # fallback (had_assistant_turn=True → success) would have lied.
        fake = _ReaderCrashProcess(stdout_lines=[
            b'{"type":"system","subtype":"init","cwd":"/x","session_id":"abc","model":"opus"}\n',
            b'{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}\n',
        ])
        req = AnalyzeRequest(
            skill_name="aist-diff-security-review",
            project_id="pipe-reader-crash",
            source_path="/tmp/p",  # noqa: S108
        )

        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
            payload = await asyncio.wait_for(main._execute_claude_skill(req), timeout=20)

        self.assertEqual(payload.status, "error")
        self.assertIn("reader crashed", payload.detail)

    async def test_reader_crash_recovers_from_valid_result_file(self):
        # The reader dies, but claude had already written its authoritative
        # result file. The bridge must consult that file (it is the real triage
        # artifact) and recover the run as success rather than failing on a
        # log-channel crash — closing the 5s _watch_result_file poll gap.
        tmpdir = Path(tempfile.mkdtemp(prefix="bridge-reader-crash-recover-"))
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        result_file = tmpdir / "result.json"
        result_file.write_text(json.dumps({"findings": []}), encoding="utf-8")

        fake = _ReaderCrashProcess(stdout_lines=[
            b'{"type":"system","subtype":"init","cwd":"/x","session_id":"abc","model":"opus"}\n',
        ])
        req = AnalyzeRequest(
            skill_name="aist-diff-security-review",
            project_id="pipe-reader-crash-recover",
            source_path="/tmp/p",  # noqa: S108
            extra_args=f"output_path={tmpdir} result_filename=result.json",
        )

        with patch("main._build_skill_prompt", return_value="prompt"), \
             patch("main.asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
             patch("main._signal_pgroup", side_effect=_fake_signal_pgroup(fake)):
            payload = await asyncio.wait_for(main._execute_claude_skill(req), timeout=20)

        self.assertEqual(payload.status, "success")


class AnalyzeRequestValidationTests(unittest.TestCase):

    """
    Pydantic-layer validation rejects malformed identifiers before they
    reach the path-construction code (fix #2 from the bridge review:
    path-traversal via ``project_id``).
    """

    def test_rejects_project_id_with_path_traversal(self):
        from pydantic import ValidationError  # noqa: PLC0415

        with self.assertRaises(ValidationError):
            AnalyzeRequest(
                skill_name="aist-finding-triage",
                project_id="../../etc/passwd",
                source_path="/tmp/x",  # noqa: S108
            )

    def test_rejects_project_id_with_slash(self):
        from pydantic import ValidationError  # noqa: PLC0415

        with self.assertRaises(ValidationError):
            AnalyzeRequest(
                skill_name="aist-finding-triage",
                project_id="abc/def",
                source_path="/tmp/x",  # noqa: S108
            )

    def test_accepts_uuid_style_pipeline_id(self):
        # Real-world value: AISTPipeline.id is a 36-char UUID with hyphens.
        req = AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="4a1912d7-90ab-4cde-8fgh-123456789012",
            source_path="/tmp/x",  # noqa: S108
        )
        self.assertEqual(req.project_id, "4a1912d7-90ab-4cde-8fgh-123456789012")

    def test_accepts_integer_string_project_id(self):
        # Real-world value from aist/tasks/claude.py (project.id as int).
        AnalyzeRequest(
            skill_name="aist-finding-triage",
            project_id="42",
            source_path="/tmp/x",  # noqa: S108
        )

    def test_rejects_bad_skill_name(self):
        from pydantic import ValidationError  # noqa: PLC0415

        with self.assertRaises(ValidationError):
            AnalyzeRequest(
                skill_name="../etc/passwd",
                project_id="42",
                source_path="/tmp/x",  # noqa: S108
            )


class PayloadFromResultEventWhitelistTests(unittest.TestCase):

    """
    Unknown subtypes are surfaced in the detail so a drifted Claude CLI
    is visible to operators (fix #7).
    """

    def test_known_success_subtype(self):
        payload = main._payload_from_result_event({"subtype": "success", "result": "done"})
        self.assertEqual(payload.status, "success")

    def test_known_error_subtype_labelled(self):
        payload = main._payload_from_result_event({
            "subtype": "error_max_turns", "result": "exceeded limit",
        })
        self.assertEqual(payload.status, "error")
        self.assertIn("error_max_turns", payload.detail)
        self.assertNotIn("subtype=", payload.detail)  # known subtypes get clean label

    def test_unknown_subtype_marked_unknown(self):
        # Hypothetical future Anthropic subtype — must NOT be silently
        # treated as success.
        payload = main._payload_from_result_event({
            "subtype": "completed_with_warnings", "result": "ok-ish",
        })
        self.assertEqual(payload.status, "error")
        self.assertIn("subtype=completed_with_warnings", payload.detail)


class ScanEnvForSecretsTests(unittest.TestCase):

    """
    Defence-in-depth env-secret scan picks up token-shaped values that
    were not passed via ``req.subprocess_env`` (fix #5).
    """

    def test_finds_anthropic_token_shape(self):
        leaked = "sk-ant-oat01-" + "A" * 30
        with patch.dict(__import__("os").environ, {"OLD_TOKEN": leaked}, clear=False):
            secrets = main._scan_env_for_anthropic_secrets()
        self.assertIn(leaked, secrets)

    def test_ignores_non_matching_values(self):
        with patch.dict(__import__("os").environ, {"NOT_A_TOKEN": "plain-string"}, clear=False):
            secrets = main._scan_env_for_anthropic_secrets()
        self.assertNotIn("plain-string", secrets)

    def test_ignores_short_anthropic_prefix(self):
        # Too few trailing chars — not a real token, must not trigger
        # the redactor (which would mask a 4-char value in every log
        # line containing those 4 chars).
        with patch.dict(__import__("os").environ, {"FAKE": "sk-ant-short"}, clear=False):
            secrets = main._scan_env_for_anthropic_secrets()
        self.assertNotIn("sk-ant-short", secrets)


class OpenPipelineLogHandlerDedupTests(unittest.TestCase):

    """
    Concurrent runs sharing a ``pipeline_id`` must not double-attach
    file handlers to the shared logger (fix #1, log-handler aspect).
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="bridge-dedup-test-")
        self._orig_log_dir = main.AIST_LOG_DIR
        main.AIST_LOG_DIR = self._tmpdir

    def tearDown(self):
        main.AIST_LOG_DIR = self._orig_log_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_returns_none_when_handler_already_attached(self):
        log = logging.getLogger("test-dedup-logger")
        # Clear any leftover handlers from earlier test runs.
        for h in list(log.handlers):
            log.removeHandler(h)
        try:
            first = main._open_pipeline_log_handler("dup-pipeline", log=log)
            self.assertIsNotNone(first)
            log.addHandler(first)
            second = main._open_pipeline_log_handler("dup-pipeline", log=log)
            self.assertIsNone(second, "second call must dedup")
        finally:
            for h in list(log.handlers):
                log.removeHandler(h)
                h.close()


class CleanupRunTasksSwallowsExceptionsTests(unittest.IsolatedAsyncioTestCase):

    """
    A drain coroutine that raises mid-loop must not block the callback
    path (fix #3 — the failure mode where the pipeline hangs in
    WAITING_RESULT_FROM_AI because cleanup propagated up).
    """

    async def test_drain_task_exception_is_swallowed(self):
        async def _raises():  # noqa: RUF029
            msg = "logging filter exploded"
            raise RuntimeError(msg)

        drain_task = asyncio.create_task(_raises())
        # Let it finish before we call cleanup so it is in ``done`` with
        # an exception attached.
        with contextlib.suppress(RuntimeError):
            await drain_task

        # Cleanup must return normally — NO RuntimeError propagated up.
        await main._cleanup_run_tasks(bg_tasks=(), drain_tasks=(drain_task,))

    async def test_drain_task_in_progress_exception_is_swallowed(self):
        # The harder path: drain_task raises while wait_for is awaiting it.
        async def _raises_eventually():
            await asyncio.sleep(0.01)
            msg = "kaboom"
            raise RuntimeError(msg)

        drain_task = asyncio.create_task(_raises_eventually())
        await main._cleanup_run_tasks(bg_tasks=(), drain_tasks=(drain_task,))


# ``contextlib`` is used inside the test above — make the import explicit
# so that running just this class via ``-k`` works without setup ordering
# surprises.
import contextlib  # noqa: E402  -- end of file

if __name__ == "__main__":
    unittest.main()
