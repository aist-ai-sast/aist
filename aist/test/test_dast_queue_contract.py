"""
What AIST accepts from the DAST gateway's self-describing queue.

The page renders whatever the gateway sends, so these tests pin the two halves of that bargain: the
payload is bounded and its addresses cannot leave the gateway, and vocabulary the gateway adds later
still renders instead of breaking the page.

Fixtures are synthetic. They are shaped like the published worked examples, never copied from a real
run: no real host, run id or verdict id belongs in a test.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from aist.integrations.dast_queue_contract import (
    DastQueueContractError,
    find_affordance,
    href_addresses_run,
    normalise_descriptor,
    normalise_page,
    normalise_refusal,
    submitted_values,
)

RUN = "2026-01-01_example_autonomous"


def _field(name: str, **overrides) -> dict:
    field = {
        "name": name, "type": "text", "required": False, "read_only": False,
        "max_length": 4000, "value": "", "values_from": "", "help": "",
    }
    field.update(overrides)
    return field


def _action(name: str, *, method: str = "POST", suffix: str = "", fields: list | None = None) -> dict:
    return {
        "name": name,
        "method": method,
        "href": f"runs/{RUN}/queue/item-1{suffix}",
        "fields": fields if fields is not None else [_field("reason", required=True)],
    }


def _descriptor(**overrides) -> dict:
    payload = {
        "contract_version": "2.0",
        "descriptor_digest": "sha256:" + "0" * 8,
        "run_state": "running",
        "capabilities": ["queue:read", "queue:control"],
        "client_hints": {"poll_interval_seconds": 60, "max_items_per_page": 100},
        "mutation_envelope": {"fields": [_field("request_id", required=True, max_length=128)]},
        "execution": {
            "claim_statuses": ["running"],
            "parallel_contracts": ["report"],
            "serial_note": "One investigating job at a time.",
            "parallel_note": "Takes no share of the reservation.",
            "session_capacity": 1,
        },
        "vocabulary": {
            "status": [{"id": "pending", "label": "Pending", "help": "Queued.", "icon": "clock",
                        "severity": "", "group": "open"}],
            "wait": [{"id": "session-capacity", "label": "No free session", "help": "Ceiling reached.",
                      "icon": "alert", "severity": "attention", "group": ""}],
            "kind": [{"id": "job", "label": "Job", "help": "Declared work.", "icon": "play",
                      "severity": "", "group": ""}],
            "phase": [{"id": "6", "label": "6", "help": "Depth.", "icon": "", "severity": "", "group": ""}],
        },
        "enums": {"skill": [{"id": "idor", "title": "idor"}], "scope": [{"id": "https://app.example", "title": "app"}]},
        "run_actions": [{"name": "pause", "method": "POST", "href": f"runs/{RUN}/queue/holds",
                         "fields": [_field("reason", required=True)]}],
        "action_catalog": {
            "cancel": {"title": "Cancel", "verb": "Cancel this work", "short_verb": "Cancel…",
                       "icon": "ban", "danger": True, "effect": "Counted as not covered."},
        },
    }
    payload.update(overrides)
    return payload


def _page(**overrides) -> dict:
    payload = {
        "run_id": RUN,
        "revision": 7,
        "state": "active",
        "run_state": "running",
        "mutations_frozen": False,
        "descriptor_digest": "sha256:" + "0" * 8,
        "total": 1,
        "uncovered": 0,
        "context_revision": 0,
        "next_offset": None,
        "counts": {"by_status": {"pending": 1}, "planned": 0, "total": 1},
        "items": [{
            "item_id": "item-1", "type": "job", "status": "pending", "wait_reason": "session-capacity",
            "phase": "6", "purpose": "check", "agent_contract": "check", "origin": "agent",
            "objective": "Prove or disprove the lead", "scope": ["https://app.example"],
            "inputs": ["recon-1"], "attempt": 0, "updated_at": "2026-01-01T00:00:00Z",
            "actions": [_action("cancel", suffix="/cancel")],
        }],
        "history": [],
        "handoffs": [],
    }
    payload.update(overrides)
    return payload


class QueuePayloadEnvelopeTests(SimpleTestCase):

    """The envelope AIST will render, and the three refusals that are load-bearing."""

    def test_descriptor_and_page_round_trip(self):
        descriptor = normalise_descriptor(_descriptor())
        page = normalise_page(_page())
        self.assertEqual(descriptor["client_hints"]["poll_interval_seconds"], 60)
        self.assertEqual(page["items"][0]["actions"][0]["name"], "cancel")
        self.assertEqual(page["counts"]["by_status"], {"pending": 1})

    def _offered(self, page: dict) -> list[str]:
        """Which actions survive validation for the first row of this page."""
        normalised = normalise_page(page)
        return [action["name"] for action in normalised["items"][0]["actions"]]

    def test_an_address_off_the_gateway_is_never_offered(self):
        # The row still renders; only the control whose address AIST will not send is withheld.
        # Dropping the action rather than the page is what keeps one malformed affordance from
        # blanking a 500-row queue, while the address still never reaches the sender.
        for href in ("https://attacker.example/steal", "/integrations/v2/runs/x/queue",
                     "//evil.example/x", "runs/../../etc/passwd", "runs/x/queue?token=1",
                     "jobs/x/cancel"):
            page = _page()
            page["items"][0]["actions"][0]["href"] = href
            self.assertEqual(self._offered(page), [], msg=href)

    def test_a_percent_encoded_traversal_is_never_offered(self):
        # `requests` decodes `%2e` while preparing the URL, so this would arrive at the gateway as
        # `..` and leave /integrations/v2/ once the server normalises it.
        for href in (f"runs/{RUN}/%2e%2e/%2e%2e/admin", f"runs/{RUN}/%2E%2E/admin",
                     f"runs/{RUN}/queue/%2f%2e%2e"):
            page = _page()
            page["items"][0]["actions"][0]["href"] = href
            self.assertEqual(self._offered(page), [], msg=href)

    def test_an_address_for_another_run_is_not_this_run_s(self):
        # The caller was authorized for one pipeline, so an affordance naming a different run is not
        # something to forward even though it is a well-formed relative address.
        self.assertTrue(href_addresses_run(f"runs/{RUN}/queue/item-1/cancel", RUN))
        self.assertFalse(href_addresses_run("runs/2026-01-02_other_autonomous/queue/x/cancel", RUN))
        self.assertFalse(href_addresses_run(f"runs/{RUN}x/queue/x/cancel", RUN))
        self.assertFalse(href_addresses_run(f"runs/{RUN}/queue/x/cancel", ""))

    def test_an_action_asking_for_a_credential_is_never_offered(self):
        page = _page()
        page["items"][0]["actions"][0]["fields"].append(_field("api_token"))
        self.assertEqual(self._offered(page), [])

    def test_a_method_this_build_will_not_send_is_never_offered(self):
        page = _page()
        page["items"][0]["actions"][0]["method"] = "PUT"
        self.assertEqual(self._offered(page), [])

    def test_a_page_longer_than_this_build_holds_is_cut_not_refused(self):
        # The envelope still reports what the run owns, so a cut page says "more than is listed"
        # while a refused one says nothing at all.
        item = _page()["items"][0]
        page = normalise_page(_page(
            items=[dict(item, item_id=f"item-{index}") for index in range(501)], total=501,
        ))
        self.assertEqual(len(page["items"]), 500)
        self.assertEqual(page["total"], 501)

    def test_vocabulary_the_gateway_adds_later_still_renders(self):
        # A status, wait reason or icon this build has never heard of is data, not an error: the
        # page shows the raw id. Refusing it would put the vocabulary back inside AIST.
        page = normalise_page(_page(items=[dict(_page()["items"][0], status="quarantined",
                                               wait_reason="waiting-for-the-moon")]))
        self.assertEqual(page["items"][0]["status"], "quarantined")
        self.assertEqual(page["items"][0]["wait_reason"], "waiting-for-the-moon")

    def test_a_field_type_this_build_cannot_render_is_marked_unrenderable(self):
        page = _page()
        page["items"][0]["actions"][0]["fields"] = [_field("schedule", type="datetime-local"),
                                                   _field("reason", required=True)]
        action = normalise_page(page)["items"][0]["actions"][0]
        self.assertFalse(action["fields"][0]["renderable"])
        self.assertTrue(action["fields"][1]["renderable"])

    def test_refusal_keeps_the_field_and_the_allowed_values(self):
        refusal = normalise_refusal({
            "code": "QUEUE_CONFLICT", "message": "ledger revision is 7, expected 5",
            "retryable": False, "correlation_id": None, "field": "expected_revision", "allowed": ["7"],
        })
        self.assertEqual(refusal["field"], "expected_revision")
        self.assertEqual(refusal["allowed"], ["7"])


class AdditiveChangeTests(SimpleTestCase):

    """
    What the gateway may change without this page going blank.

    The whole point of a self-describing queue is that the consumer does not have to be edited when
    the producer adds a value, a field or an action. These are the changes that must stay cheap, and
    the two that must not: an envelope without a revision, because a command is decided against it,
    and an address that leaves the gateway.
    """

    def test_a_vocabulary_this_build_has_never_seen_is_accepted(self):
        descriptor = _descriptor()
        descriptor["vocabulary"]["escalation"] = [
            {"id": "page-oncall", "label": "Page on-call", "help": "", "icon": "siren",
             "severity": "", "group": ""},
        ]
        descriptor["vocabulary"]["status"].append(
            {"id": "quarantined", "label": "Quarantined", "help": "", "icon": "snowflake",
             "severity": "", "group": "open"},
        )
        descriptor["budget"] = {"checks_left": 3}
        normalised = normalise_descriptor(descriptor)
        self.assertIn("escalation", normalised["vocabulary"])
        self.assertEqual(normalised["vocabulary"]["status"][-1]["icon"], "snowflake")

    def test_a_cadence_outside_this_page_s_range_is_clamped_not_refused(self):
        descriptor = _descriptor()
        descriptor["client_hints"]["poll_interval_seconds"] = 900
        descriptor["client_hints"]["max_items_per_page"] = 5000
        hints = normalise_descriptor(descriptor)["client_hints"]
        self.assertEqual(hints["poll_interval_seconds"], 600)
        self.assertEqual(hints["max_items_per_page"], 500)

    def test_prose_longer_than_expected_is_cut_rather_than_refused(self):
        page = _page()
        page["items"][0]["objective"] = "o" * 300_000
        page["items"][0]["work_subjects"] = [
            {"role": "outcome", "verdict_id": "v1", "item": "x", "target": "y",
             "status": "vuln", "conclusion": "c" * 200_000},
        ]
        item = normalise_page(page)["items"][0]
        # Each field keeps its own bound; what matters is that over-long prose is cut to it rather
        # than taking the page down with it.
        self.assertEqual(len(item["objective"]), 64 * 1024)
        self.assertEqual(len(item["work_subjects"][0]["conclusion"]), 8192)

    def test_one_unreadable_row_costs_that_row_only(self):
        page = _page(items=["not an object", _page()["items"][0]])
        normalised = normalise_page(page)
        self.assertEqual(len(normalised["items"]), 1)
        self.assertEqual(normalised["dropped_rows"], 1)

    def test_one_unsendable_action_costs_that_control_only(self):
        page = _page()
        page["items"][0]["actions"] = [
            {"name": "evil", "method": "POST", "href": "https://attacker.example/x", "fields": []},
            {"name": "trace", "method": "TRACE", "href": f"runs/{RUN}/queue/item-1", "fields": []},
            _action("cancel", suffix="/cancel"),
        ]
        item = normalise_page(page)["items"][0]
        self.assertEqual([action["name"] for action in item["actions"]], ["cancel"])
        self.assertEqual(normalise_page(page)["dropped_rows"], 0)

    def test_an_envelope_without_a_usable_revision_is_refused(self):
        # A revision is what a command is decided against. Rendering a page whose revision cannot be
        # read would offer controls that can only be refused, so this one is not negotiable.
        with self.assertRaises(DastQueueContractError):
            normalise_page(_page(revision="seven"))


class ValueBoundTests(SimpleTestCase):

    """
    What a row carries is cut to fit, never grounds to lose the row.

    AIST lays the page out, so it knows a row has `inputs` and that they read as a list. It does not
    know what the gateway lists in them, and the build that assumed cost an operator a real row: a
    job listed a value longer than the id length this contract had guessed, the whole job vanished
    from a finished run's queue while still being counted in its totals, and the only record of
    which row it had been was one line in a worker log.
    """

    def test_a_listed_value_longer_than_an_id_is_cut_rather_than_costing_the_row(self):
        page = _page()
        page["items"][0]["inputs"] = ["https://app.example/discovered?" + "q" * 4000]
        normalised = normalise_page(page)
        self.assertEqual(normalised["dropped_rows"], 0)
        self.assertEqual(len(normalised["items"][0]["inputs"][0]), 2048)

    def test_a_list_with_more_entries_than_expected_keeps_what_fits(self):
        page = _page()
        page["items"][0]["plan_rows"] = [f"row-{index}" for index in range(200)]
        normalised = normalise_page(page)
        self.assertEqual(normalised["dropped_rows"], 0)
        self.assertEqual(len(normalised["items"][0]["plan_rows"]), 128)

    def test_an_entry_that_is_not_a_value_is_left_out_and_the_row_stays(self):
        page = _page()
        page["items"][0]["scope"] = ["app.example", {"host": "api.example"}, "api.example"]
        normalised = normalise_page(page)
        self.assertEqual(normalised["dropped_rows"], 0)
        self.assertEqual(normalised["items"][0]["scope"], ["app.example", "api.example"])

    def test_null_reads_as_a_field_the_gateway_has_no_value_for(self):
        page = _page()
        page["items"][0].update({"objective": None, "attempt": None, "scope": None})
        normalised = normalise_page(page)
        item = normalised["items"][0]
        self.assertEqual(normalised["dropped_rows"], 0)
        self.assertEqual((item["objective"], item["attempt"], item["scope"]), ("", 0, []))

    def test_more_results_than_expected_cost_the_extra_ones_only(self):
        page = _page()
        page["items"][0]["work_subjects"] = [
            {"role": "outcome", "verdict_id": f"v{index}"} for index in range(80)
        ]
        normalised = normalise_page(page)
        self.assertEqual(normalised["dropped_rows"], 0)
        self.assertEqual(len(normalised["items"][0]["work_subjects"]), 64)

    def test_one_unreadable_result_costs_that_result_only(self):
        page = _page()
        page["items"][0]["work_subjects"] = ["not a record", {"role": "outcome", "verdict_id": "v1"}]
        normalised = normalise_page(page)
        self.assertEqual(normalised["dropped_rows"], 0)
        self.assertEqual(
            [subject["verdict_id"] for subject in normalised["items"][0]["work_subjects"]], ["v1"],
        )

    def test_more_actions_than_expected_cost_the_extra_controls_only(self):
        page = _page()
        page["items"][0]["actions"] = [
            _action(f"act-{index}", suffix=f"/act-{index}") for index in range(12)
        ]
        normalised = normalise_page(page)
        self.assertEqual(normalised["dropped_rows"], 0)
        self.assertEqual(len(normalised["items"][0]["actions"]), 8)

    def test_an_address_off_the_gateway_is_the_one_thing_a_cut_never_reaches(self):
        page = _page()
        page["items"][0]["actions"] = [
            {"name": "elsewhere", "method": "POST", "href": "../../other-run/queue", "fields": []},
        ]
        normalised = normalise_page(page)
        self.assertEqual(normalised["dropped_rows"], 0)
        self.assertEqual(normalised["items"][0]["actions"], [])

    def test_a_row_with_no_name_is_refused_because_an_empty_name_addresses_the_run(self):
        # A command carrying no item id is a command aimed at the run. A row kept without a name
        # would offer its own controls and quietly send them against the whole run instead.
        for missing in ({}, {"item_id": ""}, {"item_id": None}):
            with self.subTest(missing=missing):
                item = dict(_page()["items"][0])
                item.pop("item_id")
                item.update(missing)
                normalised = normalise_page(_page(items=[item]))
                self.assertEqual(normalised["items"], [])
                self.assertEqual(normalised["dropped_rows"], 1)

    def test_a_name_too_long_to_address_with_is_refused_rather_than_cut(self):
        # Cutting it would produce a name that looks addressable and resolves to the wrong row.
        page = _page()
        page["items"][0]["item_id"] = "item-" + "x" * 300
        self.assertEqual(normalise_page(page)["dropped_rows"], 1)

    def test_the_page_names_the_row_it_could_not_read(self):
        # Without the id an operator sees a tally and has no way to ask which row is missing.
        page = _page(items=[
            {"item_id": "agent-chasing-a-lead", "type": "job", "status": "completed",
             "attempt": "several"},
            _page()["items"][0],
        ])
        normalised = normalise_page(page)
        self.assertEqual(normalised["dropped_rows"], 1)
        self.assertEqual(normalised["dropped_ids"], ["agent-chasing-a-lead"])


class SubmittedValueTests(SimpleTestCase):

    """What may travel in a command: what the action declared, and nothing else."""

    def setUp(self):
        self.descriptor = normalise_descriptor(_descriptor())
        self.action = normalise_page(_page(items=[dict(_page()["items"][0], actions=[_action(
            "edit", method="PATCH", fields=[
                _field("objective", max_length=16000),
                _field("skill", type="enum", values_from="skill", max_length=128),
                _field("inputs", read_only=True, value="recon-1"),
                _field("reason", required=True),
            ],
        )])]))["items"][0]["actions"][0]

    def test_only_declared_writable_fields_travel(self):
        submitted = submitted_values(
            self.action,
            {"objective": "ask something else", "reason": "clarify", "inputs": ["smuggled"], "nope": "drop"},
            enums=self.descriptor["enums"],
        )
        self.assertEqual(submitted, {"objective": "ask something else", "reason": "clarify"})

    def test_an_option_outside_the_enum_is_refused(self):
        with self.assertRaises(DastQueueContractError):
            submitted_values(
                self.action, {"reason": "clarify", "skill": "no-such-skill"}, enums=self.descriptor["enums"],
            )

    def test_a_missing_required_field_is_refused(self):
        with self.assertRaises(DastQueueContractError):
            submitted_values(self.action, {"objective": "ask something else"}, enums=self.descriptor["enums"])

    def test_an_action_the_item_did_not_advertise_has_no_address(self):
        item = normalise_page(_page())["items"][0]
        self.assertIsNone(find_affordance(actions=item["actions"], name="delay"))
        self.assertIsNotNone(find_affordance(actions=item["actions"], name="cancel"))


class TerminalRunTests(SimpleTestCase):

    """A finished run is a record: it is readable, and it offers nothing but releasing a pause."""

    def test_a_frozen_run_passes_its_lifecycle_through(self):
        page = normalise_page(_page(
            run_state="succeeded",
            mutations_frozen=True,
            state="paused",
            items=[{
                "item_id": "hold-1", "type": "hold", "status": "active", "actor": "operator:on-call",
                "reason": "Holding while the stand is redeployed", "created_at": "2026-01-01T00:00:00Z",
                "actions": [{"name": "resume", "method": "DELETE",
                             "href": f"runs/{RUN}/queue/holds/hold-1",
                             "fields": [_field("reason", required=True)]}],
            }],
        ))
        self.assertTrue(page["mutations_frozen"])
        self.assertEqual(page["run_state"], "succeeded")
        # The queue's own state and the run's are different questions, and a finished run commonly
        # answers them differently; the page must carry both rather than infer one from the other.
        self.assertEqual(page["state"], "paused")
        self.assertEqual([action["name"] for action in page["items"][0]["actions"]], ["resume"])
