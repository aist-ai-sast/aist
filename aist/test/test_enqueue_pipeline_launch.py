import threading
from datetime import timedelta
from inspect import signature

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.execution.coalescing import canonical_coalesce_key
from aist.execution.contracts import PipelineExecutionKind
from aist.execution.enqueue import (
    LaunchEnqueueError,
    LaunchIdempotencyConflictError,
    LaunchPrincipal,
    enqueue_pipeline_launch,
)
from aist.execution.sast import SastPipelineLaunchAdapter, planning_context_from_launch_request
from aist.models import (
    AISTApiToken,
    AISTLaunchConfigAction,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    ApiTokenScope,
    LaunchSchedule,
    Organization,
    PipelineLaunchAuthorityKind,
    PipelineLaunchOrigin,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
    VersionType,
)
from aist.pipeline_args import PipelineArguments
from aist.test.test_api import AISTApiBase


class EnqueuePipelineLaunchTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name="Launch producer organization",
            product_type=self.prod_type,
        )
        self.raw_params = {
            "project_version": {"id": self.pv.id},
            "log_level": "INFO",
        }
        self.config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Producer config",
            params=self.raw_params,
        )
        self.schedule = LaunchSchedule.objects.create(
            launch_config=self.config,
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_runs=1,
        )
        self.token, _raw_token = AISTApiToken.issue(
            user=self.user,
            organization=self.organization,
            name="producer-token",
            scope=ApiTokenScope.READ_WRITE,
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def test_all_producer_principals_freeze_same_normalized_snapshot_with_explicit_audit(self):
        expected = PipelineArguments.normalize_params(project=self.project, raw_params=self.raw_params)
        producer_inputs = (
            (
                LaunchPrincipal.for_user(organization=self.organization, requester=self.user),
                {},
                "manual-ui-1",
                PipelineLaunchOrigin.MANUAL,
                PipelineLaunchAuthorityKind.USER,
            ),
            (
                LaunchPrincipal.for_user(
                    organization=self.organization,
                    requester=self.user,
                    api_token=self.token,
                ),
                {},
                "api-1",
                PipelineLaunchOrigin.MANUAL,
                PipelineLaunchAuthorityKind.PAT,
            ),
            (
                LaunchPrincipal.for_scm_webhook(organization=self.organization),
                {},
                "webhook-1",
                PipelineLaunchOrigin.SCM_WEBHOOK,
                PipelineLaunchAuthorityKind.SCM_WEBHOOK,
            ),
            (
                LaunchPrincipal.for_schedule(organization=self.organization),
                {"launch_config": self.config, "schedule": self.schedule},
                "schedule-1",
                PipelineLaunchOrigin.SCHEDULE,
                PipelineLaunchAuthorityKind.SCHEDULE,
            ),
        )

        requests = []
        for principal, relations, client_key, origin, authority_kind in producer_inputs:
            result = enqueue_pipeline_launch(
                project=self.project,
                principal=principal,
                raw_params=self.raw_params if not relations else {},
                execution_type=PipelineExecutionKind.SAST,
                client_request_key=client_key,
                **relations,
            )
            self.assertTrue(result.created)
            self.assertEqual(result.request.params_snapshot, expected)
            self.assertEqual(result.request.origin, origin)
            self.assertEqual(result.request.authority_kind, authority_kind)
            requests.append(result.request)

        self.assertEqual(PipelineLaunchRequest.objects.filter(pk__in=[item.pk for item in requests]).count(), 4)

    def test_client_request_key_is_idempotent_and_conflicting_reuse_is_rejected(self):
        principal = LaunchPrincipal.for_user(organization=self.organization, requester=self.user)
        first = enqueue_pipeline_launch(
            project=self.project,
            principal=principal,
            raw_params=self.raw_params,
            client_request_key="stable-client-key",
        )
        replay = enqueue_pipeline_launch(
            project=self.project,
            principal=principal,
            raw_params=self.raw_params,
            client_request_key="stable-client-key",
        )

        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(replay.request.pk, first.request.pk)
        self.assertNotIn("stable-client-key", first.request.client_request_key_hash)
        with self.assertRaises(LaunchIdempotencyConflictError):
            enqueue_pipeline_launch(
                project=self.project,
                principal=principal,
                raw_params={**self.raw_params, "log_level": "DEBUG"},
                client_request_key="stable-client-key",
            )

    def test_initial_launch_data_is_frozen_and_sensitive_fields_are_rejected(self):
        principal = LaunchPrincipal.for_user(organization=self.organization, requester=self.user)
        launch_data = {
            "one_off_actions": [{"id": "action-1", "config": {"channel": "alerts"}}],
            "one_off_actions_done": [],
        }

        result = enqueue_pipeline_launch(
            project=self.project,
            principal=principal,
            raw_params=self.raw_params,
            client_request_key="launch-data-1",
            initial_launch_data=launch_data,
        )
        launch_data["one_off_actions"][0]["config"]["channel"] = "mutated"

        self.assertEqual(
            result.request.initial_launch_data_snapshot["one_off_actions"][0]["config"]["channel"],
            "alerts",
        )
        with self.assertRaises(LaunchEnqueueError):
            enqueue_pipeline_launch(
                project=self.project,
                principal=principal,
                raw_params=self.raw_params,
                client_request_key="launch-data-secret",
                initial_launch_data={"one_off_actions": [{"config": {"api_token": "secret"}}]},
            )

    def test_launch_config_actions_are_frozen_in_the_request(self):
        action = AISTLaunchConfigAction.objects.create(
            launch_config=self.config,
            trigger_status="FINISHED",
            action_type=AISTLaunchConfigAction.ActionType.WRITE_LOG,
            config={"level": "INFO", "description": "Original"},
        )

        request = enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_user(
                organization=self.organization,
                requester=self.user,
            ),
            raw_params={},
            launch_config=self.config,
        ).request
        action.config = {"level": "ERROR", "description": "Changed later"}
        action.save(update_fields=["config", "updated"])

        snapshot = request.initial_launch_data_snapshot["launch_config_actions"]
        self.assertEqual(snapshot[0]["id"], str(action.pk))
        self.assertEqual(snapshot[0]["config"]["description"], "Original")

    def test_principal_and_relations_are_server_validated(self):
        other_organization = Organization.objects.create(
            name="Other launch organization",
            product_type=self.other_prod_type,
        )
        with self.assertRaises(LaunchEnqueueError):
            enqueue_pipeline_launch(
                project=self.project,
                principal=LaunchPrincipal.for_schedule(organization=other_organization),
                raw_params=self.raw_params,
            )

        parameters = signature(enqueue_pipeline_launch).parameters
        self.assertNotIn("state", parameters)
        self.assertNotIn("resource_key", parameters)
        self.assertNotIn("coalesce_key", parameters)
        self.assertNotIn("authority_kind", parameters)
        self.assertNotIn("requester_id", parameters)
        self.assertNotIn("api_token_id", parameters)

    def test_new_equivalent_request_supersedes_only_pending_equivalent_requests(self):
        principal = LaunchPrincipal.for_user(organization=self.organization, requester=self.user)
        first = enqueue_pipeline_launch(
            project=self.project,
            principal=principal,
            raw_params=self.raw_params,
            client_request_key="coalesce-first",
        ).request
        claimed = enqueue_pipeline_launch(
            project=self.project,
            principal=principal,
            raw_params={**self.raw_params, "log_level": "DEBUG"},
            client_request_key="coalesce-claimed",
        ).request
        PipelineLaunchRequest.objects.filter(pk=claimed.pk).update(state=PipelineLaunchRequestState.CLAIMED)

        replacement = enqueue_pipeline_launch(
            project=self.project,
            principal=principal,
            raw_params=self.raw_params,
            client_request_key="coalesce-replacement",
        ).request

        first.refresh_from_db()
        claimed.refresh_from_db()
        self.assertEqual(first.state, PipelineLaunchRequestState.SUPERSEDED)
        self.assertEqual(first.superseded_by_id, replacement.pk)
        self.assertEqual(first.failure_code, "SUPERSEDED")
        self.assertIn(str(replacement.pk), first.failure_detail)
        self.assertEqual(claimed.state, PipelineLaunchRequestState.CLAIMED)
        self.assertIsNone(claimed.superseded_by_id)

    def test_canonical_key_changes_for_binding_identity_or_parameters(self):
        base = {
            "execution_type": PipelineExecutionKind.DAST,
            "project_id": self.project.pk,
            "params_snapshot": {"depth": 2},
            "capability_snapshot": {"revision": "cap-7"},
        }
        binding_one = canonical_coalesce_key(executor_identity={"binding_id": 11}, **base)
        binding_two = canonical_coalesce_key(executor_identity={"binding_id": 12}, **base)
        changed_params = canonical_coalesce_key(
            executor_identity={"binding_id": 11},
            **{**base, "params_snapshot": {"depth": 3}},
        )

        self.assertNotEqual(binding_one, binding_two)
        self.assertNotEqual(binding_one, changed_params)

    def test_manual_run_of_a_scheduled_config_uses_the_schedules_coalesce_key_at_enqueue_and_plan_time(self):
        """
        Regression: a manual "run now" against a launch_config that has its own attached
        LaunchSchedule (self.config/self.schedule from setUp) draws from that schedule's
        capacity slot, not a separate per-project pool — enqueue_pipeline_launch is called
        here exactly as LaunchConfigStartAPI.post calls it, with no explicit schedule=,
        relying on the launch_config fallback to resolve it the same way build_plan does.
        """
        principal = LaunchPrincipal.for_user(organization=self.organization, requester=self.user)
        request = enqueue_pipeline_launch(
            project=self.project,
            principal=principal,
            raw_params=self.raw_params,
            launch_config=self.config,
        ).request

        plan = SastPipelineLaunchAdapter().build_plan(planning_context_from_launch_request(request))

        self.assertEqual(request.coalesce_key, plan.coalesce_key)
        self.assertEqual(plan.resource_key, f"sast-schedule:{self.schedule.pk}")


class ConcurrentEnqueueCoalescingTests(TransactionTestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="coalesce-user")
        sla = SLA_Configuration.objects.create(name="Coalescing SLA")
        product_type = Product_Type.objects.create(name="Coalescing product type")
        organization = Organization.objects.create(name="Coalescing organization", product_type=product_type)
        product = Product.objects.create(
            name="Coalescing product",
            description="",
            prod_type=product_type,
            sla_configuration=sla,
        )
        project = AISTProject.objects.create(product=product)
        version = AISTProjectVersion.objects.create(
            project=project,
            version_type=VersionType.GIT_HASH,
            version="coalesce-sha",
        )
        self.user_id = user.pk
        self.organization_id = organization.pk
        self.project_id = project.pk
        self.params = {"project_version": {"id": version.pk}}

    def test_concurrent_equivalent_enqueue_leaves_one_pending_request(self):
        barrier = threading.Barrier(2)
        request_ids: list[int] = []
        errors: list[Exception] = []

        def enqueue(index: int) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                result = enqueue_pipeline_launch(
                    project=AISTProject.objects.get(pk=self.project_id),
                    principal=LaunchPrincipal.for_user(
                        organization=Organization.objects.get(pk=self.organization_id),
                        requester=get_user_model().objects.get(pk=self.user_id),
                    ),
                    raw_params=self.params,
                    client_request_key=f"concurrent-{index}",
                )
                request_ids.append(result.request.pk)
            except Exception as exc:  # pragma: no cover - asserted below with full exception detail
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=enqueue, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads), "enqueue must not deadlock")
        self.assertEqual(errors, [])
        self.assertEqual(len(request_ids), 2)
        requests = PipelineLaunchRequest.objects.filter(pk__in=request_ids)
        self.assertEqual(requests.filter(state=PipelineLaunchRequestState.PENDING).count(), 1)
        self.assertEqual(requests.filter(state=PipelineLaunchRequestState.SUPERSEDED).count(), 1)
