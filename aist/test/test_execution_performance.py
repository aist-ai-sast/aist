from __future__ import annotations

import re
from pathlib import Path

import yaml
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from aist.execution.claiming import claim_next_launch_request
from aist.models import (
    Organization,
    PipelineLaunchAuthorityKind,
    PipelineLaunchOrigin,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.test.test_api import AISTApiBase

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _compose_default(value: str) -> int:
    match = re.fullmatch(r"\$\{[A-Z0-9_]+:-(\d+)\}", value)
    if match is None:
        message = f"Expected a numeric Compose default, got {value!r}"
        raise AssertionError(message)
    return int(match.group(1))


class ExecutionPerformanceConfigurationTests(SimpleTestCase):

    def test_launch_queue_has_the_dispatch_access_index(self):
        indexes = {
            index.name: tuple(index.fields)
            for index in PipelineLaunchRequest._meta.indexes
        }

        self.assertEqual(
            indexes["aist_launch_req_dispatch_idx"],
            ("state", "not_before", "priority"),
        )

    def test_long_dast_uses_the_shared_worker_with_reserved_parallel_capacity(self):
        compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        services = compose["services"]
        self.assertNotIn("dast-worker", services)
        self.assertNotIn("dast-queue", services)

        worker_environment = services["celeryworker"]["environment"]
        concurrency = _compose_default(worker_environment["DD_CELERY_WORKER_CONCURRENCY"])
        autoscale_minimum = _compose_default(worker_environment["DD_CELERY_WORKER_AUTOSCALE_MIN"])
        prefetch = _compose_default(worker_environment["DD_CELERY_WORKER_PREFETCH_MULTIPLIER"])

        self.assertGreaterEqual(concurrency, 2)
        self.assertGreaterEqual(autoscale_minimum, 2)
        self.assertEqual(prefetch, 1)


class LaunchClaimQueryBudgetTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        Organization.objects.create(
            name="Performance acceptance organization",
            product_type=self.prod_type,
        )

    def test_large_queue_claim_uses_an_index_with_constant_query_count(self):
        now = timezone.now()
        PipelineLaunchRequest.objects.bulk_create([
            PipelineLaunchRequest(
                project=self.project,
                origin=PipelineLaunchOrigin.RECONCILER,
                authority_kind=PipelineLaunchAuthorityKind.RECONCILER,
                state=PipelineLaunchRequestState.DISPATCHED,
                not_before=now,
            )
            for _index in range(2000)
        ])
        ready = PipelineLaunchRequest.objects.create(
            project=self.project,
            origin=PipelineLaunchOrigin.RECONCILER,
            authority_kind=PipelineLaunchAuthorityKind.RECONCILER,
            state=PipelineLaunchRequestState.PENDING,
            not_before=now,
            priority=100,
        )
        ready_query = PipelineLaunchRequest.objects.filter(
            state=PipelineLaunchRequestState.PENDING,
            not_before__lte=now,
        ).order_by("-priority", "created", "pk")

        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
        query_plan = ready_query.explain()
        with CaptureQueriesContext(connection) as queries:
            claimed = claim_next_launch_request(claim_owner="performance-acceptance", now=now)

        self.assertIn("Index Scan", query_plan)
        self.assertEqual(claimed.request_id, ready.pk)
        self.assertLessEqual(
            len(queries),
            8,
            f"Claim query count grew with queue depth: {[query['sql'] for query in queries]}",
        )
