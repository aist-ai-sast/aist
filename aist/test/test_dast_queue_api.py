"""
The tenant-scoped proxy over a DAST run's queue.

The page it serves renders whatever the gateway sends, so the endpoint's own job is narrow and
testable: scope the pipeline to the caller's tenant, pass the gateway's payload through, and -- for a
command -- take the address from the affordance the gateway published rather than from the browser.

Fixtures are synthetic and shaped like the published worked examples; no real host, run id or verdict
id appears here.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.cache import cache, caches
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product_Type_Member, Role

from aist.integrations.dast_config import DastTargetSnapshot
from aist.integrations.dast_gateway_client import (
    DastGatewayClientError,
    DastQueueCommandResult,
    DastQueueDocument,
)
from aist.models import (
    AISTPipeline,
    AISTStatus,
    DastExecutionState,
    DastIntegrationState,
    DastIntegrationValidationState,
    DastProjectBinding,
    DastQueueSnapshot,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionType,
)
from aist.services.dast_queue import (
    DastQueueError,
    apply_command,
    queue_is_addressable,
    refresh_snapshot,
)
from aist.services.dast_targets import refresh_dast_targets
from aist.tasks.dast_queue import apply_dast_queue_command, refresh_dast_queue
from aist.test.dast_fixtures import perimeter_target_wire
from aist.test.test_api import AISTApiBase
from aist.utils.cache import QUEUE_CACHE_ALIAS

RUN = "2026-01-01_example_autonomous"


def _field(name: str, **overrides) -> dict:
    field = {
        "name": name, "type": "text", "required": False, "read_only": False,
        "max_length": 4000, "value": "", "values_from": "", "help": "",
    }
    field.update(overrides)
    return field


def _descriptor(capabilities: list[str] | None = None, run_actions: list | None = None) -> dict:
    return {
        "contract_version": "2.0",
        "descriptor_digest": "sha256:digest",
        "run_state": "running",
        "capabilities": capabilities if capabilities is not None else ["queue:read", "queue:control"],
        "client_hints": {"poll_interval_seconds": 60, "max_items_per_page": 100},
        "mutation_envelope": {"fields": [_field("request_id", required=True, max_length=128)]},
        "execution": {"claim_statuses": ["running"], "parallel_contracts": ["report"],
                      "serial_note": "One at a time.", "parallel_note": "Alongside.", "session_capacity": 1},
        "vocabulary": {
            "status": [{"id": "pending", "label": "Pending", "help": "Queued.", "icon": "clock",
                        "severity": "", "group": "open"}],
            "wait": [], "kind": [], "phase": [],
        },
        "enums": {"skill": [{"id": "idor", "title": "idor"}]},
        "run_actions": run_actions if run_actions is not None else [],
        "action_catalog": {"cancel": {"title": "Cancel", "verb": "Cancel this work",
                                      "short_verb": "Cancel…", "icon": "ban", "danger": True,
                                      "effect": "Counted as not covered."}},
    }


def _page(items: list | None = None, **overrides) -> dict:
    payload = {
        "run_id": RUN, "revision": 7, "state": "active", "run_state": "running",
        "mutations_frozen": False, "descriptor_digest": "sha256:digest", "total": 1,
        "uncovered": 0, "context_revision": 0, "next_offset": None,
        "counts": {"by_status": {"pending": 1}, "planned": 0, "total": 1},
        "items": items if items is not None else [_item()],
        "history": [], "handoffs": [],
    }
    payload.update(overrides)
    return payload


def _item(**overrides) -> dict:
    item = {
        "item_id": "item-1", "type": "job", "status": "pending", "wait_reason": "",
        "phase": "6", "purpose": "check", "agent_contract": "check", "origin": "agent",
        "objective": "Prove or disprove the lead", "scope": ["https://app.example"],
        "inputs": ["recon-1"], "attempt": 0, "updated_at": "2026-01-01T00:00:00Z",
        "actions": [{
            "name": "cancel", "method": "POST",
            "href": f"runs/{RUN}/queue/item-1/cancel",
            "fields": [_field("reason", required=True)],
        }],
    }
    item.update(overrides)
    return item


class FakeGateway:

    """Stands in for the gateway, and records exactly what AIST asked it to do."""

    def __init__(self, *, descriptor=None, page=None, command=None, read_error=""):
        self._descriptor = descriptor if descriptor is not None else _descriptor()
        self._page = page if page is not None else _page()
        self._command = command
        self._read_error = read_error
        self.sent: list[dict] = []

    def queue_descriptor(self, run_id, *, etag=""):
        if self._read_error:
            raise DastGatewayClientError(self._read_error)
        return DastQueueDocument(payload=self._descriptor, etag="descriptor")

    def queue_page(self, run_id, **kwargs):
        if self._read_error:
            raise DastGatewayClientError(self._read_error)
        return DastQueueDocument(payload=self._page, etag=str(self._page["revision"]))

    def queue_item(self, run_id, item_id):
        if self._read_error:
            raise DastGatewayClientError(self._read_error)
        items = [item for item in self._page["items"] if item["item_id"] == item_id]
        return DastQueueDocument(payload=dict(self._page, items=items), etag="item")

    def queue_command(self, *, run_id, method, href, body):
        self.sent.append({"run_id": run_id, "method": method, "href": href, "body": body})
        if self._command is not None:
            return self._command
        return DastQueueCommandResult(accepted=True, payload={
            "request_id": body["request_id"], "actor": "aist:tester", "action": "cancel",
            "item_id": "item-1", "reason": body.get("reason", ""), "created_at": "2026-01-01T00:01:00Z",
            "before_revision": 7, "revision": 8, "status": "applied",
        })


@contextmanager
def _as_gateway(fake):
    yield fake


class DastQueueAPITests(AISTApiBase):

    def setUp(self):
        super().setUp()
        # The throttle's cache and the queue's own store are different aliases; both carry state
        # from one test into the next.
        cache.clear()
        caches[QUEUE_CACHE_ALIAS].clear()
        self.organization = Organization.objects.create(name="Queue org", product_type=self.prod_type)
        self.integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST",
            config={
                "gateway_url": "https://gateway.example",
                "ca_bundle": "",
                "contract_major": 2,
                "integrator_public_id": "public-id",
                "server_fingerprint": "sha256:server-fingerprint",
            },
            secret="runtime-token",  # noqa: S106 -- test fixture
            is_active=True,
        )
        DastIntegrationState.objects.create(
            integration=self.integration,
            validation_state=DastIntegrationValidationState.READY,
            validated_at=timezone.now(),
            contract_version="2.0",
            capabilities_etag="catalog-1",
            capabilities_synced_at=timezone.now(),
        )
        self.target = refresh_dast_targets(
            self.integration,
            (DastTargetSnapshot.from_snapshot(perimeter_target_wire("perimeter")),),
            seen_at=timezone.now(),
        )[0]
        self.binding = DastProjectBinding.objects.create(
            project=self.project, target=self.target, enabled=True, parameter_snapshot={},
        )
        self.pipeline = AISTPipeline.objects.create(
            id="dastqueue1",
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.EXECUTING,
            dast_binding=self.binding,
        )
        DastExecutionState.objects.create(pipeline=self.pipeline, run_id=RUN)
        self.read_url = reverse("aist_api:pipeline_dast_queue", args=[self.pipeline.id])
        self.command_url = reverse("aist_api:pipeline_dast_queue_command", args=[self.pipeline.id])

    def _patch(self, fake):
        def scoped(*_args, **_kwargs):
            return _as_gateway(fake)

        return patch("aist.services.dast_queue.scoped_dast_gateway_client", scoped)

    def _snapshot(self, **overrides):
        defaults = {
            "run_id": RUN,
            "descriptor": _descriptor(),
            "page": _page(),
            "etag": "7",
            "descriptor_digest": "sha256:digest",
            "fetched_at": timezone.now(),
        }
        defaults.update(overrides)
        return DastQueueSnapshot.objects.create(pipeline=self.pipeline, **defaults)

    def test_a_read_serves_the_snapshot_without_reaching_the_gateway(self):
        # The route to a gateway needs the worker's Docker socket and VPN sidecar. A request path
        # that tried would fail in production, so this asserts it never tries.
        self._snapshot()
        with patch("aist.services.dast_queue.scoped_dast_gateway_client", side_effect=AssertionError):
            response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["page"]["revision"], 7)
        self.assertEqual(body["descriptor"]["capabilities"], ["queue:read", "queue:control"])

    def test_a_pipeline_with_no_snapshot_yet_says_it_is_reading(self):
        with patch("aist.tasks.dast_queue.refresh_dast_queue.delay") as asked:
            response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["reading"])
        asked.assert_called_once_with(str(self.pipeline.id))

    def test_a_pipeline_still_waiting_for_its_first_snapshot_asks_once(self):
        # While the gateway is down nothing ever gets stored, so every poll of every open tab would
        # otherwise cost a worker another VPN-routed call.
        with patch("aist.tasks.dast_queue.refresh_dast_queue.delay") as asked:
            for _ in range(10):
                self.assertTrue(self.client.get(self.read_url).json()["reading"])
        self.assertEqual(asked.call_count, 1)

    def test_a_stale_snapshot_is_shown_while_a_fresh_read_is_asked_for(self):
        self._snapshot(fetched_at=timezone.now() - timedelta(minutes=10))
        with patch("aist.tasks.dast_queue.refresh_dast_queue.delay") as asked:
            body = self.client.get(self.read_url).json()
        self.assertTrue(body["available"])
        self.assertTrue(body["stale"])
        asked.assert_called_once()

    def test_a_finished_run_is_not_read_again(self):
        # Its queue is a record: nothing in it will move, so asking the gateway again buys nothing.
        frozen = _page(mutations_frozen=True, run_state="succeeded", state="paused")
        self._snapshot(page=frozen, fetched_at=timezone.now() - timedelta(days=2))
        with patch("aist.tasks.dast_queue.refresh_dast_queue.delay") as asked:
            body = self.client.get(self.read_url).json()
        self.assertTrue(body["available"])
        self.assertTrue(body["page"]["mutations_frozen"])
        asked.assert_not_called()

    def test_a_run_the_gateway_no_longer_serves_degrades_quietly(self):
        # An old run whose target left the token's allowlist answers UNKNOWN_RUN. For a finished
        # pipeline that is expected, so the block says so rather than showing an error.
        self._snapshot(page={}, descriptor={}, unavailable_code="UNKNOWN_RUN",
                       unavailable_detail="This run's queue is no longer available to AIST.")
        body = self.client.get(self.read_url).json()
        self.assertFalse(body["available"])
        self.assertEqual(body["unavailable"]["code"], "UNKNOWN_RUN")

    def test_a_running_pipeline_whose_run_id_has_not_arrived_says_so(self):
        # AIST records the provider's run id when its connector reports it, which for a run still in
        # flight may not have happened yet. The block is still offered and names what it waits for.
        DastExecutionState.objects.filter(pipeline=self.pipeline).update(run_id="")
        self.assertTrue(queue_is_addressable(AISTPipeline.objects.get(pk=self.pipeline.pk)))
        body = self.client.get(self.read_url).json()
        self.assertFalse(body["available"])
        self.assertEqual(body["unavailable"]["code"], "RUN_NOT_REPORTED")

    def test_a_finished_pipeline_that_never_recorded_a_run_offers_no_block(self):
        DastExecutionState.objects.filter(pipeline=self.pipeline).update(run_id="")
        self.pipeline.status = AISTStatus.FINISHED
        self.pipeline.save(update_fields=["status"])
        self.assertFalse(queue_is_addressable(AISTPipeline.objects.get(pk=self.pipeline.pk)))
        self.assertEqual(self.client.get(self.read_url).json()["unavailable"]["code"], "NO_RUN")

    def test_another_tenants_pipeline_is_not_found(self):
        Product_Type_Member.objects.filter(product_type=self.prod_type, user=self.user).delete()
        self.assertEqual(self.client.get(self.read_url).status_code, 404)

    def test_the_worker_stores_what_the_gateway_answered(self):
        fake = FakeGateway()
        with self._patch(fake):
            refresh_snapshot(self.pipeline)
        snapshot = DastQueueSnapshot.objects.get(pipeline=self.pipeline)
        self.assertEqual(snapshot.run_id, RUN)
        self.assertEqual(snapshot.page["revision"], 7)
        self.assertEqual(snapshot.source, DastQueueSnapshot.SOURCE_GATEWAY)

    def test_an_unavailable_run_is_stored_as_the_answer_it_is(self):
        fake = FakeGateway(read_error="UNKNOWN_RUN")
        with self._patch(fake):
            refresh_snapshot(self.pipeline)
        snapshot = DastQueueSnapshot.objects.get(pipeline=self.pipeline)
        self.assertEqual(snapshot.unavailable_code, "UNKNOWN_RUN")

    def test_a_failed_read_leaves_the_snapshot_an_operator_already_had(self):
        self._snapshot()
        fake = FakeGateway(read_error="GATEWAY_UNREACHABLE")
        with self._patch(fake):
            refresh_dast_queue(str(self.pipeline.id))
        snapshot = DastQueueSnapshot.objects.get(pipeline=self.pipeline)
        self.assertEqual(snapshot.page["revision"], 7)
        self.assertEqual(snapshot.unavailable_code, "")

    def _command(self, **overrides):
        body = {
            "action": "cancel", "item_id": "item-1", "request_id": "aist-1",
            "expected_revision": 7, "values": {"reason": "Out of scope for this window"},
        }
        body.update(overrides)
        return body

    def _apply(self, fake, **overrides):
        """Run one command the way the worker does, from the arguments the endpoint hands it."""
        sent = self._command(**overrides)
        with self._patch(fake):
            return apply_command(
                self.pipeline,
                action=sent["action"],
                item_id=sent["item_id"],
                values=sent["values"],
                request_id=sent["request_id"],
                expected_revision=sent["expected_revision"],
                actor="aist:tester",
            )

    def test_a_command_is_accepted_and_handed_to_a_worker(self):
        with patch("aist.tasks.dast_queue.apply_dast_queue_command.delay") as sent:
            response = self.client.post(self.command_url, self._command(), format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"request_id": "aist-1", "state": "queued"})
        pipeline_id, = sent.call_args.args
        self.assertEqual(pipeline_id, str(self.pipeline.id))
        self.assertEqual(sent.call_args.kwargs["action"], "cancel")
        self.assertEqual(sent.call_args.kwargs["values"], {"reason": "Out of scope for this window"})
        self.assertEqual(sent.call_args.kwargs["actor"], self.user.username)

    def test_the_page_can_collect_what_its_command_produced(self):
        with patch("aist.tasks.dast_queue.apply_dast_queue_command.delay"):
            self.client.post(self.command_url, self._command(), format="json")
        outcome_url = reverse(
            "aist_api:pipeline_dast_queue_command_outcome", args=[self.pipeline.id, "aist-1"],
        )
        # Until the worker answers, the page is told to keep waiting rather than that it failed.
        self.assertEqual(self.client.get(outcome_url).json()["state"], "queued")

        fake = FakeGateway()
        with self._patch(fake):
            apply_dast_queue_command(
                str(self.pipeline.id), action="cancel", item_id="item-1",
                values={"reason": "Withdrawn"}, request_id="aist-1",
                expected_revision=7, actor=self.user.username,
            )
        body = self.client.get(outcome_url).json()
        self.assertEqual(body["state"], "done")
        self.assertTrue(body["accepted"])
        self.assertEqual(body["receipt"]["revision"], 8)

    def test_a_command_uses_the_address_the_gateway_published(self):
        fake = FakeGateway()
        # A browser cannot choose where the command goes: it names an action and an item, and the
        # worker reads the method and the address off the gateway's own affordance.
        outcome = self._apply(fake)
        self.assertTrue(outcome.accepted)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["method"], "POST")
        self.assertEqual(fake.sent[0]["href"], f"runs/{RUN}/queue/item-1/cancel")
        self.assertEqual(fake.sent[0]["body"]["expected_revision"], 7)
        self.assertEqual(fake.sent[0]["body"]["reason"], "Out of scope for this window")

    def test_an_address_the_browser_supplies_is_ignored(self):
        with patch("aist.tasks.dast_queue.apply_dast_queue_command.delay") as sent:
            self.client.post(self.command_url, dict(self._command(),
                                                    href="https://attacker.example/steal",
                                                    method="DELETE"), format="json")
        self.assertNotIn("href", sent.call_args.kwargs)
        self.assertNotIn("method", sent.call_args.kwargs)

    def test_a_field_the_action_did_not_declare_never_travels(self):
        fake = FakeGateway()
        self._apply(fake, values={"reason": "Withdrawn", "objective": "something else entirely"})
        self.assertNotIn("changes", fake.sent[0]["body"])
        self.assertEqual(set(fake.sent[0]["body"]), {"request_id", "expected_revision", "reason"})

    def test_an_action_the_item_does_not_offer_is_refused_without_calling_out(self):
        fake = FakeGateway()
        with self.assertRaises(DastQueueError) as refused:
            self._apply(fake, action="delay", values={"reason": "Carry it over"})
        self.assertEqual(refused.exception.code, "QUEUE_ACTION_NOT_OFFERED")
        self.assertEqual(fake.sent, [])

    def test_a_read_only_caller_is_offered_nothing_to_send(self):
        # A caller holding only queue:read is served rows with no actions, which is the correct
        # rendering; a command it invents anyway has no address to go to.
        fake = FakeGateway(
            descriptor=_descriptor(capabilities=["queue:read"]),
            page=_page(items=[_item(actions=[])]),
        )
        with self.assertRaises(DastQueueError):
            self._apply(fake, values={"reason": "Withdrawn"})
        self.assertEqual(fake.sent, [])

    def test_an_affordance_aimed_at_another_run_is_not_forwarded(self):
        # A payload for this pipeline advertising another run's address would reach a run the caller
        # never passed an authorization check for.
        other = _item(actions=[{
            "name": "cancel", "method": "POST",
            "href": "runs/2026-01-02_other_autonomous/queue/item-1/cancel",
            "fields": [_field("reason", required=True)],
        }])
        fake = FakeGateway(page=_page(items=[other]))
        with self.assertRaises(DastQueueError) as refused:
            self._apply(fake, values={"reason": "Withdrawn"})
        self.assertEqual(refused.exception.code, "QUEUE_ACTION_NOT_OFFERED")
        self.assertEqual(fake.sent, [])

    def test_a_command_on_a_pipeline_that_never_ran_is_not_found(self):
        DastExecutionState.objects.filter(pipeline=self.pipeline).update(run_id="")
        AISTPipeline.objects.filter(pk=self.pipeline.pk).update(status=AISTStatus.FINISHED)
        with patch("aist.tasks.dast_queue.apply_dast_queue_command.delay") as sent:
            response = self.client.post(self.command_url, self._command(), format="json")
        self.assertEqual(response.status_code, 404)
        # A pipeline with no run to address answers like any other missing thing on this API; the
        # gateway's own refusals are a different kind of answer and arrive through the outcome.
        self.assertIn("detail", response.json())
        self.assertNotIn("refusal", response.json())
        sent.assert_not_called()

    def test_a_command_sent_before_the_run_id_arrives_is_not_a_dead_end(self):
        # The read path calls this state RUN_NOT_REPORTED and keeps serving the pipeline, so the
        # command path must not tell the same caller the pipeline is gone.
        DastExecutionState.objects.filter(pipeline=self.pipeline).update(run_id="")
        with patch("aist.tasks.dast_queue.apply_dast_queue_command.delay") as sent:
            response = self.client.post(self.command_url, self._command(), format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get(self.read_url).json()["unavailable"]["code"],
                         "RUN_NOT_REPORTED")
        sent.assert_not_called()

    def test_a_refusal_this_endpoint_has_no_answer_for_is_not_dressed_up_as_a_404(self):
        # Only two refusals are AIST's own to make. A third one would be a new condition that has
        # to be given its own answer, rather than quietly telling the caller the pipeline is gone.
        with patch("aist.api.dast_queue.enqueue_command",
                   side_effect=DastQueueError("SOMETHING_NEW", "a condition this build predates")):
            response = self.client.post(self.command_url, self._command(), format="json")
        self.assertEqual(response.status_code, 500)

    def test_a_command_body_cannot_be_grown_without_bound(self):
        # Whatever a form declares, the worker sends only the fields the gateway published. These
        # bounds stop a body that no action could use from reaching the broker at all.
        with patch("aist.tasks.dast_queue.apply_dast_queue_command.delay") as sent:
            oversized = self.client.post(
                self.command_url,
                self._command(values={f"field-{index}": "x" for index in range(21)}),
                format="json",
            )
            nested = self.client.post(
                self.command_url, self._command(values={"reason": {"text": "withdrawn"}}),
                format="json",
            )
            long_text = self.client.post(
                self.command_url, self._command(values={"reason": "x" * 4097}), format="json",
            )
        self.assertEqual(oversized.status_code, 400)
        self.assertEqual(nested.status_code, 400)
        self.assertEqual(long_text.status_code, 400)
        sent.assert_not_called()

    def test_collecting_an_answer_does_not_spend_what_the_next_command_needs(self):
        # A page collects one answer about twenty times. On a shared budget an operator's own
        # polling would lock them out of the queue they are working.
        outcome_url = reverse(
            "aist_api:pipeline_dast_queue_command_outcome", args=[self.pipeline.id, "aist-1"],
        )
        rates = dict(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
                     aist_dast_queue="2/hour", aist_dast_queue_poll="100/hour")
        with override_settings(REST_FRAMEWORK=dict(settings.REST_FRAMEWORK,
                                                   DEFAULT_THROTTLE_RATES=rates)):
            for _ in range(20):
                self.assertEqual(self.client.get(outcome_url).status_code, 200)
            with patch("aist.tasks.dast_queue.apply_dast_queue_command.delay"):
                accepted = self.client.post(self.command_url, self._command(), format="json")
        self.assertEqual(accepted.status_code, 202)

    def test_a_command_the_broker_never_took_leaves_no_one_waiting(self):
        # If the command was never published, nothing will ever answer it. The page is told the
        # send failed; saying "queued" anyway would leave it collecting an outcome for ten minutes
        # for a command that does not exist.
        with patch("aist.tasks.dast_queue.apply_dast_queue_command.delay",
                   side_effect=OSError("broker unreachable")):
            response = self.client.post(self.command_url, self._command(), format="json")
        self.assertEqual(response.status_code, 500)
        outcome_url = reverse(
            "aist_api:pipeline_dast_queue_command_outcome", args=[self.pipeline.id, "aist-1"],
        )
        self.assertEqual(self.client.get(outcome_url).json()["state"], "unknown")

    def test_a_receipt_already_collected_is_not_reverted_by_a_retried_click(self):
        # The same request id may arrive twice -- a reloaded page, a double click. The gateway
        # treats the repeat as the same decision, and so must the page.
        fake = FakeGateway()
        with self._patch(fake):
            apply_dast_queue_command(
                str(self.pipeline.id), action="cancel", item_id="item-1",
                values={"reason": "Withdrawn"}, request_id="aist-1",
                expected_revision=7, actor=self.user.username,
            )
        with patch("aist.tasks.dast_queue.apply_dast_queue_command.delay"):
            self.client.post(self.command_url, self._command(), format="json")
        outcome_url = reverse(
            "aist_api:pipeline_dast_queue_command_outcome", args=[self.pipeline.id, "aist-1"],
        )
        body = self.client.get(outcome_url).json()
        self.assertEqual(body["state"], "done")
        self.assertTrue(body["accepted"])

    def test_a_request_id_is_collectable_whatever_the_browser_minted(self):
        # The id is the browser's to choose and is keyed by digest, so no punctuation in it can run
        # into the next field of the key -- or into what a cache backend accepts as a key at all.
        awkward = "aist 1:2 queued"
        fake = FakeGateway()
        with self._patch(fake):
            apply_dast_queue_command(
                str(self.pipeline.id), action="cancel", item_id="item-1",
                values={"reason": "Withdrawn"}, request_id=awkward,
                expected_revision=7, actor=self.user.username,
            )
        outcome_url = reverse(
            "aist_api:pipeline_dast_queue_command_outcome", args=[self.pipeline.id, awkward],
        )
        body = self.client.get(outcome_url).json()
        self.assertEqual(body["state"], "done")
        self.assertTrue(body["accepted"])

    def test_a_cache_that_is_down_does_not_take_the_page_with_it(self):
        # The shared store can be unreachable while the rest of the platform is up. Everything kept
        # in it is recoverable -- another read, another wait -- so nothing here may fail a request.
        self._snapshot(fetched_at=timezone.now() - timedelta(minutes=10))
        unreachable = MagicMock()
        for operation in ("get", "set", "add"):
            getattr(unreachable, operation).side_effect = OSError("valkey is down")
        with (
            patch("aist.services.dast_queue.caches", {QUEUE_CACHE_ALIAS: unreachable}),
            patch("aist.tasks.dast_queue.refresh_dast_queue.delay") as asked,
            patch("aist.tasks.dast_queue.apply_dast_queue_command.delay") as sent,
        ):
            read = self.client.get(self.read_url)
            command = self.client.post(self.command_url, self._command(), format="json")
            outcome = self.client.get(reverse(
                "aist_api:pipeline_dast_queue_command_outcome", args=[self.pipeline.id, "aist-1"],
            ))
        self.assertEqual(read.status_code, 200)
        self.assertTrue(read.json()["available"])
        asked.assert_called_once_with(str(self.pipeline.id))
        self.assertEqual(command.status_code, 202)
        sent.assert_called_once()
        self.assertEqual(outcome.status_code, 200)
        self.assertEqual(outcome.json()["state"], "unknown")

    def test_a_transport_failure_reaches_the_page_as_a_refusal(self):
        fake = FakeGateway(read_error="GATEWAY_UNREACHABLE")
        with self._patch(fake):
            apply_dast_queue_command(
                str(self.pipeline.id), action="cancel", item_id="item-1",
                values={"reason": "Withdrawn"}, request_id="aist-9",
                expected_revision=7, actor=self.user.username,
            )
        outcome_url = reverse(
            "aist_api:pipeline_dast_queue_command_outcome", args=[self.pipeline.id, "aist-9"],
        )
        body = self.client.get(outcome_url).json()
        self.assertFalse(body["accepted"])
        self.assertEqual(body["refusal"]["code"], "GATEWAY_UNREACHABLE")

    def test_one_operator_does_not_collect_another_s_receipt(self):
        # request_id is minted by the browser. Without the operator in the key, a reader of this
        # pipeline could collect -- or overwrite -- a receipt someone else is waiting for, reason
        # and all.
        fake = FakeGateway()
        with self._patch(fake):
            apply_dast_queue_command(
                str(self.pipeline.id), action="cancel", item_id="item-1",
                values={"reason": "Withdrawn"}, request_id="aist-shared",
                expected_revision=7, actor="another.operator",
            )
        outcome_url = reverse(
            "aist_api:pipeline_dast_queue_command_outcome", args=[self.pipeline.id, "aist-shared"],
        )
        body = self.client.get(outcome_url).json()
        self.assertEqual(body["state"], "unknown")
        self.assertEqual(body["receipt"], {})

    def test_a_refusal_is_returned_with_its_field_and_allowed_values(self):
        fake = FakeGateway(command=DastQueueCommandResult(accepted=False, payload={
            "code": "QUEUE_CONFLICT", "message": "ledger revision is 9, expected 7",
            "retryable": False, "correlation_id": None, "field": "expected_revision", "allowed": ["9"],
        }))
        outcome = self._apply(fake, values={"reason": "Withdrawn"})
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.refusal["code"], "QUEUE_CONFLICT")
        self.assertEqual(outcome.refusal["field"], "expected_revision")
        self.assertEqual(outcome.refusal["allowed"], ["9"])

    def test_a_command_is_followed_by_a_fresh_read(self):
        # What the command did is only visible in the queue it changed.
        fake = FakeGateway()
        with self._patch(fake), patch("aist.tasks.dast_queue.refresh_dast_queue.delay") as asked:
            apply_dast_queue_command(
                str(self.pipeline.id), action="cancel", item_id="item-1",
                values={"reason": "Withdrawn"}, request_id="aist-6",
                expected_revision=7, actor="aist:tester",
            )
        asked.assert_called_once_with(str(self.pipeline.id))

    def test_a_finished_run_is_readable_and_offers_only_its_pause(self):
        hold = {
            "item_id": "hold-1", "type": "hold", "status": "active", "actor": "operator:on-call",
            "reason": "Holding while the stand is redeployed", "created_at": "2026-01-01T00:00:00Z",
            "actions": [{"name": "resume", "method": "DELETE", "href": f"runs/{RUN}/queue/holds/hold-1",
                         "fields": [_field("reason", required=True)]}],
        }
        self._snapshot(page=_page(
            items=[_item(actions=[]), hold], run_state="succeeded", mutations_frozen=True, state="paused",
        ))
        body = self.client.get(self.read_url).json()
        self.assertTrue(body["available"])
        self.assertTrue(body["page"]["mutations_frozen"])
        self.assertEqual(body["page"]["run_state"], "succeeded")
        offered = [action["name"] for item in body["page"]["items"] for action in item["actions"]]
        self.assertEqual(offered, ["resume"])

    def test_ten_open_tabs_ask_a_worker_once(self):
        self._snapshot(fetched_at=timezone.now() - timedelta(minutes=10))
        with patch("aist.tasks.dast_queue.refresh_dast_queue.delay") as asked:
            for _ in range(10):
                self.client.get(self.read_url)
            self.assertEqual(asked.call_count, 1)
            # The operator's own Refresh is not debounced: they asked for it.
            self.client.get(self.read_url, {"force": "true"})
        self.assertEqual(asked.call_count, 2)


class DastQueueBlockPresenceTests(AISTApiBase):

    """The pipeline page offers the block only for a DAST pipeline that names a run."""

    def setUp(self):
        super().setUp()
        self.role_maintainer, _ = Role.objects.get_or_create(
            id=Roles.Maintainer, defaults={"name": "Maintainer"},
        )

    def test_a_sast_pipeline_has_no_queue_to_address(self):
        pipeline = AISTPipeline.objects.create(
            id="sastpipe1", project=self.project, project_version=self.pv,
            status=AISTStatus.EXECUTING, execution_type=PipelineExecutionType.SAST,
        )
        self.assertFalse(queue_is_addressable(pipeline))
