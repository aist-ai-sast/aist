"""
Unit tests for aist-triage-bridge.

Runs standalone: `python -m unittest tools/aist-triage-bridge/test_main.py`
(with ``tools/aist-triage-bridge`` on ``sys.path``). The bridge ships in its
own container so it is not picked up by the Django test runner.
"""
from __future__ import annotations

import asyncio
import signal
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent))

import main
from main import AnalyzeRequest


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


class FakeProcess:

    """
    Minimal ``asyncio.subprocess.Process`` stand-in for bridge tests.

    Stdout/stderr stay open (mirroring a CLI that lingers after emitting its
    final result). ``send_signal`` optionally triggers process exit to
    simulate well-behaved vs stuck CLIs.
    """

    def __init__(self, stdout_lines, stderr_lines=(), *, exits_on_sigterm=True):
        self.pid = 12345
        self.returncode: int | None = None
        self.signals: list[int] = []
        self._exit_event = asyncio.Event()
        self._exits_on_sigterm = exits_on_sigterm
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


if __name__ == "__main__":
    unittest.main()
