from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.models import Count, DateTimeField, OuterRef, Q, Subquery
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding, Test
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from aist.queries import get_authorized_aist_pipelines

if TYPE_CHECKING:
    from rest_framework.response import Response


class AISTPipelineSummaryAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs) -> Response:
        qs = (
            get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
            .select_related("project", "project__product")
            .order_by("-created")
        )
        qp = request.query_params

        product_id = qp.get("product_id")
        status = qp.get("status")
        created_gte = qp.get("created_gte")
        created_lte = qp.get("created_lte")
        search = (qp.get("search") or "").strip()
        ordering = qp.get("ordering")

        if product_id:
            qs = qs.filter(project__product_id=product_id)
        if status:
            qs = qs.filter(status=status)
        if created_gte:
            qs = qs.filter(created__gte=created_gte)
        if created_lte:
            qs = qs.filter(created__lte=created_lte)
        if search:
            qs = qs.filter(
                Q(tests__branch_tag__icontains=search)
                | Q(tests__commit_hash__icontains=search),
            )

        qs = qs.distinct()

        if ordering in {"created", "-created", "updated", "-updated"}:
            qs = qs.order_by(ordering)

        latest_test = (
            Test.objects.filter(aist_pipelines=OuterRef("pk"))
            .order_by("-target_end", "-target_start", "-id")
        )
        qs = qs.annotate(
            branch_tag=Subquery(latest_test.values("branch_tag")[:1]),
            commit_hash=Subquery(latest_test.values("commit_hash")[:1]),
            target_start=Subquery(
                latest_test.values("target_start")[:1],
                output_field=DateTimeField(),
            ),
            target_end=Subquery(
                latest_test.values("target_end")[:1],
                output_field=DateTimeField(),
            ),
        )

        paginator = LimitOffsetPagination()
        page = paginator.paginate_queryset(qs, request)
        pipeline_ids = [pipeline.id for pipeline in page]

        counts: dict[str, int] = {}
        if pipeline_ids:
            counts_qs = (
                Finding.objects.filter(test__aist_pipelines__id__in=pipeline_ids)
                .order_by()
                .values("test__aist_pipelines__id")
                .annotate(total=Count("id"))
            )
            counts = {row["test__aist_pipelines__id"]: row["total"] for row in counts_qs}

        results: list[dict[str, Any]] = []
        for pipeline in page:
            action_runs = (pipeline.launch_data or {}).get("action_runs") or []
            actions = [
                {
                    "source": item.get("source"),
                    "type": item.get("action_type"),
                    "status": item.get("status"),
                    "updated": item.get("updated_at"),
                }
                for item in action_runs
            ]
            results.append(
                {
                    "id": pipeline.id,
                    "status": pipeline.status,
                    "project_id": pipeline.project_id,
                    "product_id": pipeline.project.product_id,
                    "product_name": pipeline.project.product.name,
                    "started": pipeline.started,
                    "created": pipeline.created,
                    "updated": pipeline.updated,
                    "branch": pipeline.branch_tag,
                    "commit": pipeline.commit_hash,
                    "findings": counts.get(pipeline.id, 0),
                    "actions": actions,
                },
            )

        return paginator.get_paginated_response(results)
