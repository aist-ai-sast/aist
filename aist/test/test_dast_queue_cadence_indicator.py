"""
The DAST run queue shows that it keeps itself up to date, and when it next will.

Two signals were settled on in the queue design and both are easy to lose in a refactor,
because nothing fails when they go: the run still polls, it just stops saying so, which
from the operator's seat is indistinguishable from a refresh that never runs.

The wait for the next read is a bar filling under the Refresh button rather than a second
ticking number, and a read the operator did not ask for announces what it brought.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

_AIST = Path(__file__).resolve().parent.parent
_QUEUE = _AIST / "templates" / "aist" / "_dast_queue.html"
_STYLESHEET = _AIST / "tailwind.css"

_REFRESH_BUTTON = re.compile(r'<button[^>]*id="dq-refresh"[^>]*>(.*?)</button>', re.DOTALL)


class QueueCadenceIndicatorTests(SimpleTestCase):
    def setUp(self):
        self.queue = _QUEUE.read_text(encoding="utf-8")
        self.stylesheet = _STYLESHEET.read_text(encoding="utf-8")

    def test_the_refresh_button_carries_the_bar(self):
        button = _REFRESH_BUTTON.search(self.queue)
        self.assertIsNotNone(button, "the queue no longer has a Refresh button")
        self.assertIn('id="dq-tick"', button.group(1))
        self.assertIn("refresh", _REFRESH_BUTTON.search(self.queue).group(0))

    def test_the_bar_has_a_track_to_fill(self):
        """Without position and overflow on the button the bar is not drawn inside it."""
        self.assertIn(".btn.refresh", self.stylesheet)
        self.assertIn(".btn.refresh > .tick", self.stylesheet)
        for rule in ("position: relative", "overflow: hidden"):
            self.assertIn(rule, self.stylesheet)

    def test_the_bar_is_driven_by_the_wait_for_the_next_read(self):
        self.assertIn('$("dq-tick")', self.queue)
        self.assertRegex(self.queue, r"waited\s*/\s*state\.pollSeconds")

    def test_the_bar_is_empty_when_no_read_is_coming(self):
        """A finished run is read once, and a background tab is not read at all."""
        self.assertRegex(self.queue, r"frozen\(\)\s*\|\|\s*document\.hidden\s*\?\s*0")


class AutomaticReadAnnouncementTests(SimpleTestCase):

    """A read nobody asked for says what it brought, so it is visibly not a dead page."""

    def setUp(self):
        self.queue = _QUEUE.read_text(encoding="utf-8")

    def test_an_automatic_read_that_changed_something_announces_it(self):
        self.assertIn('note("Updated automatically: "', self.queue)

    def test_an_automatic_read_that_changed_nothing_stays_quiet(self):
        """Announcing every poll would turn the cadence into noise."""
        self.assertRegex(self.queue, r"\}\s*else if \(moved\)\s*\{")

    def test_a_manual_re_read_reports_either_way(self):
        for wording in ('"Re-read: now at revision "', '"Re-read: no change, still revision "'):
            self.assertIn(wording, self.queue)

    def test_what_moved_is_derived_from_the_items_not_the_revision_alone(self):
        self.assertIn("function whatMoved(before, after)", self.queue)
        self.assertRegex(self.queue, r"was\[item\.item_id\] !== item\.status")


class UnansweredRefreshTests(SimpleTestCase):

    """
    A refresh that never lands is said out loud.

    A worker stamps the snapshot even when the gateway answers "unchanged", so the same read
    time coming back twice means the gateway was not reached at all. Without counting those,
    the page promises "a fresh read is on its way" indefinitely, and a queue nobody is reading
    is indistinguishable from a queue with nothing to report -- which is how a stale queue
    went unnoticed for hours.
    """

    def setUp(self):
        self.queue = _QUEUE.read_text(encoding="utf-8")

    def test_an_ask_that_brought_the_same_read_time_is_counted(self):
        self.assertRegex(self.queue, r"payload\.fetched_at === state\.fetchedAt")
        self.assertIn("state.unanswered += 1", self.queue)

    def test_a_read_that_landed_clears_the_count(self):
        self.assertIn("state.unanswered = 0", self.queue)

    def test_the_page_stops_promising_a_read_is_coming(self):
        self.assertIn("the gateway has not been re-read", self.queue)
        said = self.queue.index("the gateway has not been re-read")
        promised = self.queue.index("a fresh read is on its way")
        self.assertLess(said, promised, "the optimistic branch would win and the warning never show")

    def test_one_slow_ask_is_not_called_a_failure(self):
        self.assertRegex(self.queue, r"UNANSWERED_BEFORE_SAYING_SO = [2-9]")


class StaleAnswerFollowUpTests(SimpleTestCase):

    """
    A read that came back stale is looked at again shortly, not a whole interval later.

    Reading the gateway is handed to a worker and takes about ten seconds, while the answer the
    page gets back is the snapshot from before that read. Without a follow-up the operator sits
    in front of old data for the rest of the poll interval and Refresh is the only thing that
    appears to work -- which is exactly how this was reported.
    """

    def setUp(self):
        self.queue = _QUEUE.read_text(encoding="utf-8")

    def test_a_stale_answer_schedules_another_look(self):
        self.assertIn("function scheduleFollowUp()", self.queue)
        self.assertRegex(self.queue, r"setTimeout\(function \(\) \{ read\(false, true\); \}, FOLLOW_UP_MS\)")

    def test_the_follow_up_covers_a_gateway_read(self):
        """Four seconds apart, four times over, against a read that takes about ten."""
        follow_up_ms = int(re.search(r"FOLLOW_UP_MS = (\d+)", self.queue).group(1))
        attempts = int(re.search(r"FOLLOW_UP_ATTEMPTS = (\d+)", self.queue).group(1))
        self.assertGreaterEqual(follow_up_ms * attempts, 12000)

    def test_following_up_spends_the_budget_rather_than_renewing_it(self):
        """Renewing it on every stale answer would poll forever against a silent gateway."""
        self.assertIn("followUpsLeft = followingUp ? followUpsLeft - 1 : FOLLOW_UP_ATTEMPTS;", self.queue)

    def test_no_follow_up_where_no_read_is_coming(self):
        self.assertRegex(
            self.queue,
            r"\(!state\.stale && !state\.reading\) \|\| frozen\(\) \|\| document\.hidden"
            r" \|\| followUpsLeft <= 0",
        )


class FollowUpCoverageTests(SimpleTestCase):

    """
    The follow-up covers the waits it was added for, and says nothing about the healthy ones.

    Three ways it went wrong on first writing, each of which leaves the page looking either
    dead or alarmed while the worker is simply doing its job.
    """

    def setUp(self):
        self.queue = _QUEUE.read_text(encoding="utf-8")

    def test_a_follow_up_does_not_count_as_an_unanswered_ask(self):
        """It looks back seconds after the ask, long before a gateway read can land."""
        guarded = re.search(r"\} else if \(!followingUp\) \{(.*?)\n                \}", self.queue, re.DOTALL)
        self.assertIsNotNone(guarded, "the increment is no longer guarded by !followingUp")
        self.assertIn("state.unanswered += 1;", guarded.group(1))

    def test_a_first_read_with_no_snapshot_is_followed_up(self):
        """The longest wait of all, and the one most easily taken for a dead page."""
        unavailable = re.search(r"if \(!payload\.available\) \{(.*?)\n                \}", self.queue, re.DOTALL)
        self.assertIsNotNone(unavailable)
        self.assertIn("scheduleFollowUp();", unavailable.group(1))

    def test_a_read_in_flight_is_reason_enough_to_look_again(self):
        self.assertRegex(self.queue, r"\(!state\.stale && !state\.reading\)")

    def test_a_postponed_follow_up_reschedules_itself(self):
        """Only one of the two dialogs resumes a deferred read when it closes."""
        deferred = re.search(r"state\.deferred = true;(.*?)return;", self.queue, re.DOTALL)
        self.assertIsNotNone(deferred)
        self.assertIn("scheduleFollowUp();", deferred.group(1))
