import threading
from datetime import timedelta

from django.db import close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, Product_Type_Member, SLA_Configuration

from aist.execution.claiming import (
    AUTHORITY_REVOKED,
    claim_next_launch_request,
    revalidate_claimed_authority,
)
from aist.execution.enqueue import LaunchPrincipal, enqueue_pipeline_launch
from aist.models import (
    AISTApiToken,
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
    ProjectAccessDenial,
    VersionType,
)
from aist.test.test_api import AISTApiBase


class LaunchRequestAuthorityRevalidationTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name="Claim authority organization",
            product_type=self.prod_type,
        )
        self.params = {"project_version": {"id": self.pv.pk}}

    def _claim(self, request: PipelineLaunchRequest, owner: str = "dispatcher-a") -> None:
        claimed = claim_next_launch_request(claim_owner=owner)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.request_id, request.pk)

    def test_active_user_authority_is_valid_after_claim(self):
        request = enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_user(organization=self.organization, requester=self.user),
            raw_params=self.params,
        ).request
        self._claim(request)

        self.assertTrue(revalidate_claimed_authority(request_id=request.pk, claim_owner="dispatcher-a"))
        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.CLAIMED)
        self.assertIsNone(request.pipeline_id)

    def test_membership_revoked_between_enqueue_and_claim_fails_without_pipeline(self):
        request = enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_user(organization=self.organization, requester=self.user),
            raw_params=self.params,
        ).request
        Product_Type_Member.objects.filter(product_type=self.prod_type, user=self.user).delete()
        self._claim(request)

        self.assertFalse(revalidate_claimed_authority(request_id=request.pk, claim_owner="dispatcher-a"))
        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.FAILED)
        self.assertEqual(request.failure_code, AUTHORITY_REVOKED)
        self.assertIsNone(request.pipeline_id)

    def test_project_denial_added_between_enqueue_and_claim_fails_closed(self):
        request = enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_user(organization=self.organization, requester=self.user),
            raw_params=self.params,
        ).request
        ProjectAccessDenial.objects.create(project=self.project, user=self.user)
        self._claim(request)

        self.assertFalse(revalidate_claimed_authority(request_id=request.pk, claim_owner="dispatcher-a"))
        request.refresh_from_db()
        self.assertEqual(request.failure_code, AUTHORITY_REVOKED)

    def test_pat_scope_downgrade_between_enqueue_and_claim_fails_closed(self):
        token, _raw = AISTApiToken.issue(
            user=self.user,
            organization=self.organization,
            name="claim-token",
            scope=ApiTokenScope.READ_WRITE,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        request = enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_user(
                organization=self.organization,
                requester=self.user,
                api_token=token,
            ),
            raw_params=self.params,
        ).request
        AISTApiToken.objects.filter(pk=token.pk).update(scope=ApiTokenScope.READ_ONLY)
        self._claim(request)

        self.assertFalse(revalidate_claimed_authority(request_id=request.pk, claim_owner="dispatcher-a"))
        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.FAILED)
        self.assertEqual(request.failure_code, AUTHORITY_REVOKED)

    def test_pat_revocation_between_enqueue_and_claim_fails_closed(self):
        token, _raw = AISTApiToken.issue(
            user=self.user,
            organization=self.organization,
            name="revoked-claim-token",
            scope=ApiTokenScope.READ_WRITE,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        request = enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_user(
                organization=self.organization,
                requester=self.user,
                api_token=token,
            ),
            raw_params=self.params,
        ).request
        AISTApiToken.objects.filter(pk=token.pk).update(revoked_at=timezone.now())
        self._claim(request)

        self.assertFalse(revalidate_claimed_authority(request_id=request.pk, claim_owner="dispatcher-a"))
        request.refresh_from_db()
        self.assertEqual(request.failure_code, AUTHORITY_REVOKED)

    def test_disabled_schedule_authority_fails_closed(self):
        config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Claim schedule config",
            params=self.params,
        )
        schedule = LaunchSchedule.objects.create(
            launch_config=config,
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_runs=1,
        )
        request = enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_schedule(organization=self.organization),
            raw_params={},
            launch_config=config,
            schedule=schedule,
        ).request
        LaunchSchedule.objects.filter(pk=schedule.pk).update(enabled=False)
        self._claim(request)

        self.assertFalse(revalidate_claimed_authority(request_id=request.pk, claim_owner="dispatcher-a"))
        request.refresh_from_db()
        self.assertEqual(request.failure_code, AUTHORITY_REVOKED)

    def test_webhook_authority_requires_current_project_repository(self):
        request = enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_scm_webhook(organization=self.organization),
            raw_params=self.params,
            trigger_project_version=self.pv,
        ).request
        self._claim(request)

        self.assertFalse(revalidate_claimed_authority(request_id=request.pk, claim_owner="dispatcher-a"))
        request.refresh_from_db()
        self.assertEqual(request.failure_code, AUTHORITY_REVOKED)

    def test_wrong_claim_owner_cannot_revalidate_or_mutate_request(self):
        request = enqueue_pipeline_launch(
            project=self.project,
            principal=LaunchPrincipal.for_user(organization=self.organization, requester=self.user),
            raw_params=self.params,
        ).request
        self._claim(request)

        self.assertFalse(revalidate_claimed_authority(request_id=request.pk, claim_owner="dispatcher-b"))
        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.CLAIMED)


class ConcurrentLaunchRequestClaimTests(TransactionTestCase):
    def setUp(self):
        sla = SLA_Configuration.objects.create(name="Claim concurrency SLA")
        product_type = Product_Type.objects.create(name="Claim concurrency product type")
        Organization.objects.create(name="Claim concurrency organization", product_type=product_type)
        product = Product.objects.create(
            name="Claim concurrency product",
            description="",
            prod_type=product_type,
            sla_configuration=sla,
        )
        project = AISTProject.objects.create(product=product)
        version = AISTProjectVersion.objects.create(
            project=project,
            version_type=VersionType.GIT_HASH,
            version="claim-sha",
        )
        self.request_id = PipelineLaunchRequest.objects.create(
            project=project,
            trigger_project_version=version,
            origin=PipelineLaunchOrigin.RECONCILER,
            authority_kind=PipelineLaunchAuthorityKind.RECONCILER,
        ).pk

    def test_two_workers_never_claim_the_same_request(self):
        barrier = threading.Barrier(2)
        results = []

        def claim(owner: str) -> None:
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                results.append(claim_next_launch_request(claim_owner=owner))
            finally:
                close_old_connections()

        threads = [threading.Thread(target=claim, args=(f"worker-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads), "claim must not deadlock")
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].request_id, self.request_id)
