import threading
from datetime import timedelta

from django.db import close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.execution.contracts import (
    EffectiveVersionPolicy,
    ExecutionPlan,
    LaunchAuthority,
    LaunchAuthorityKind,
    LaunchSource,
    PipelineExecutionKind,
    PipelineTaskName,
)
from aist.execution.leases import (
    ExecutionLeaseError,
    ExecutionLeasePolicy,
    acquire_execution_plan_lease,
    release_execution_lease,
    report_stale_execution_leases,
)
from aist.models import (
    AISTProject,
    AISTProjectVersion,
    DastProjectBinding,
    DastTarget,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionLease,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
    VersionType,
)
from aist.test.test_api import AISTApiBase


class StaleExecutionLeaseReportingTests(AISTApiBase):

    """
    A lease nobody renews keeps occupying its slot until the request reconciler decides what to
    do with it. These tests pin the monitoring signal that makes that state visible, and pin
    that reporting never releases capacity on its own.
    """

    def setUp(self):
        super().setUp()
        self.request = PipelineLaunchRequest.objects.create(project=self.project)
        self.policy = ExecutionLeasePolicy(
            ttl=timedelta(minutes=5),
            heartbeat_grace=timedelta(minutes=1),
        )

    def _lease(self, *, resource_key, heartbeat_age, expiry_age):
        base_time = timezone.now()
        return base_time, PipelineExecutionLease.objects.create(
            request=self.request,
            resource_key=resource_key,
            slot=0,
            acquired_at=base_time - timedelta(minutes=10),
            heartbeat_at=base_time - heartbeat_age,
            expires_at=base_time - expiry_age,
        )

    def test_a_lease_inside_its_heartbeat_grace_is_not_reported_stale(self):
        base_time, _lease = self._lease(
            resource_key="dast:integration:18",
            heartbeat_age=timedelta(seconds=30),
            expiry_age=timedelta(seconds=1),
        )

        self.assertEqual(report_stale_execution_leases(policy=self.policy, now=base_time), 0)

    def test_a_lease_past_its_heartbeat_grace_is_reported_without_being_released(self):
        base_time, lease = self._lease(
            resource_key="dast:integration:18",
            heartbeat_age=timedelta(seconds=30),
            expiry_age=timedelta(seconds=1),
        )
        later = base_time + timedelta(minutes=2)

        self.assertEqual(report_stale_execution_leases(policy=self.policy, now=later), 1)
        lease.refresh_from_db()
        self.assertIsNone(
            lease.released_at,
            "reporting must not free capacity; only the request reconciler may release a lease",
        )

    def test_a_released_lease_is_never_reported(self):
        base_time, lease = self._lease(
            resource_key="sast:published-outbox",
            heartbeat_age=timedelta(minutes=10),
            expiry_age=timedelta(minutes=1),
        )
        lease.released_at = base_time
        lease.save(update_fields=["released_at"])

        self.assertEqual(report_stale_execution_leases(policy=self.policy, now=base_time), 0)


class ExecutionPlanLeaseTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name="Execution plan lease organization",
            product_type=self.prod_type,
        )
        integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="Lease test DAST integration",
        )
        target = DastTarget.objects.create(
            integration=integration,
            provider_id="lease-target",
            display_name="Lease target",
            contract_revision="2.0",
            capability_revision="lease-capability",
            schema_digest="lease-schema",
            parameter_schema={"type": "object"},
            repository_keys=["repository"],
            autonomous_ready=True,
            last_seen_at=timezone.now(),
        )
        self.dast_binding = DastProjectBinding.objects.create(
            project=self.project,
            target=target,
            source_repo_key="repository",
        )

    def _plan(self, *, execution_type, resource_key, resource_limit):
        is_sast = execution_type == PipelineExecutionKind.SAST
        return ExecutionPlan(
            execution_type=execution_type,
            task_name=PipelineTaskName.RUN_PIPELINE_EXECUTION,
            task_args=(),
            project_id=self.project.pk,
            trigger_project_version_id=None if is_sast else self.pv.pk,
            effective_version_policy=(
                EffectiveVersionPolicy.PRESELECT_EFFECTIVE_VERSION
                if is_sast
                else EffectiveVersionPolicy.RESOLVE_FROM_EXECUTION_RESULT
            ),
            effective_project_version_id=self.pv.pk if is_sast else None,
            resource_key=resource_key,
            resource_limit=resource_limit,
            coalesce_key="plan-lease-test",
            initial_launch_data={},
            authority=LaunchAuthority(
                kind=LaunchAuthorityKind.SCHEDULE,
                source=LaunchSource.SCHEDULE,
                organization_id=self.organization.pk,
            ),
        )

    def _claimed_request(self, *, execution_type, owner="lease-dispatcher"):
        return PipelineLaunchRequest.objects.create(
            project=self.project,
            execution_type=execution_type.value,
            dast_binding=self.dast_binding if execution_type == PipelineExecutionKind.DAST else None,
            trigger_project_version=self.pv if execution_type == PipelineExecutionKind.DAST else None,
            state=PipelineLaunchRequestState.CLAIMED,
            claim_owner=owner,
            claimed_at=timezone.now(),
        )

    def test_same_dast_integration_resource_serializes_different_requests(self):
        plan = self._plan(
            execution_type=PipelineExecutionKind.DAST,
            resource_key="dast:integration:91",
            resource_limit=1,
        )
        first_request = self._claimed_request(execution_type=PipelineExecutionKind.DAST)
        second_request = self._claimed_request(execution_type=PipelineExecutionKind.DAST)

        first = acquire_execution_plan_lease(
            request_id=first_request.pk,
            claim_owner="lease-dispatcher",
            plan=plan,
        )
        blocked = acquire_execution_plan_lease(
            request_id=second_request.pk,
            claim_owner="lease-dispatcher",
            plan=plan,
        )

        self.assertIsNotNone(first)
        self.assertIsNone(blocked)
        self.assertTrue(release_execution_lease(lease_id=first.pk, request_id=first_request.pk))
        self.assertIsNotNone(acquire_execution_plan_lease(
            request_id=second_request.pk,
            claim_owner="lease-dispatcher",
            plan=plan,
        ))

    def test_sast_plan_capacity_and_claim_owner_are_enforced(self):
        plan = self._plan(
            execution_type=PipelineExecutionKind.SAST,
            resource_key="sast-worker",
            resource_limit=2,
        )
        requests = [self._claimed_request(execution_type=PipelineExecutionKind.SAST) for _ in range(3)]

        self.assertIsNotNone(acquire_execution_plan_lease(
            request_id=requests[0].pk,
            claim_owner="lease-dispatcher",
            plan=plan,
        ))
        self.assertIsNotNone(acquire_execution_plan_lease(
            request_id=requests[1].pk,
            claim_owner="lease-dispatcher",
            plan=plan,
        ))
        self.assertIsNone(acquire_execution_plan_lease(
            request_id=requests[2].pk,
            claim_owner="lease-dispatcher",
            plan=plan,
        ))
        with self.assertRaises(ExecutionLeaseError):
            acquire_execution_plan_lease(
                request_id=requests[2].pk,
                claim_owner="other-dispatcher",
                plan=plan,
            )


class PipelineExecutionLeaseConcurrencyTests(TransactionTestCase):

    CLAIM_OWNER = "lease-concurrency-dispatcher"
    RESOURCE_KEY = "dast:integration:concurrent"

    def setUp(self):
        product_type = Product_Type.objects.create(name="Lease concurrency PT")
        sla = SLA_Configuration.objects.create(name="Lease concurrency SLA")
        self.organization = Organization.objects.create(
            name="Lease concurrency org",
            product_type=product_type,
        )
        product = Product.objects.create(
            name="Lease concurrency product",
            description="",
            prod_type=product_type,
            sla_configuration=sla,
        )
        self.project = AISTProject.objects.create(product=product)
        self.project_version = AISTProjectVersion.objects.create(
            project=self.project,
            version="main",
            version_type=VersionType.GIT_BRANCH,
        )
        integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="Concurrent lease DAST integration",
        )
        target = DastTarget.objects.create(
            integration=integration,
            provider_id="concurrent-lease-target",
            display_name="Concurrent lease target",
            contract_revision="2.0",
            capability_revision="concurrent-lease-capability",
            schema_digest="concurrent-lease-schema",
            parameter_schema={"type": "object"},
            repository_keys=["repository"],
            autonomous_ready=True,
            last_seen_at=timezone.now(),
        )
        self.dast_binding = DastProjectBinding.objects.create(
            project=self.project,
            target=target,
            source_repo_key="repository",
        )
        self.request_ids = [
            PipelineLaunchRequest.objects.create(
                project=self.project,
                execution_type=PipelineExecutionKind.DAST.value,
                dast_binding=self.dast_binding,
                trigger_project_version=self.project_version,
                state=PipelineLaunchRequestState.CLAIMED,
                claim_owner=self.CLAIM_OWNER,
                claimed_at=timezone.now(),
            ).id
            for _index in range(8)
        ]

    def _plan(self):
        return ExecutionPlan(
            execution_type=PipelineExecutionKind.DAST,
            task_name=PipelineTaskName.RUN_PIPELINE_EXECUTION,
            task_args=(),
            project_id=self.project.pk,
            trigger_project_version_id=self.project_version.pk,
            effective_version_policy=EffectiveVersionPolicy.RESOLVE_FROM_EXECUTION_RESULT,
            effective_project_version_id=None,
            resource_key=self.RESOURCE_KEY,
            resource_limit=1,
            coalesce_key="lease-concurrency",
            initial_launch_data={},
            authority=LaunchAuthority(
                kind=LaunchAuthorityKind.SCHEDULE,
                source=LaunchSource.SCHEDULE,
                organization_id=self.organization.pk,
            ),
        )

    def test_concurrent_capacity_one_has_exactly_one_winner(self):
        barrier = threading.Barrier(2)
        results: dict[int, int | None] = {}
        plan = self._plan()

        def acquire(request_id: int) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                lease = acquire_execution_plan_lease(
                    request_id=request_id,
                    claim_owner=self.CLAIM_OWNER,
                    plan=plan,
                )
                results[request_id] = lease.id if lease else None
            finally:
                close_old_connections()

        threads = [threading.Thread(target=acquire, args=(request_id,)) for request_id in self.request_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads), "lease acquisition must not deadlock")
        self.assertEqual(sum(lease_id is not None for lease_id in results.values()), 1)
        self.assertEqual(
            PipelineExecutionLease.objects.filter(
                resource_key=self.RESOURCE_KEY,
                released_at__isnull=True,
            ).count(),
            1,
        )
