from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db.models import Count, DateTimeField, OuterRef, Q, Subquery
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding

from aist.api.common import API_SEVERITY_VALUES, empty_severity_counts
from aist.models import AISTPipeline, AISTStatus
from aist.queries import get_authorized_aist_pipelines, get_authorized_aist_projects, get_authorized_findings
from aist.utils.project_version_refs import resolve_project_version_git_refs

PIPELINE_ORDERING = {"created", "-created", "updated", "-updated"}
PIPELINE_STATUS = {status for status, _label in AISTStatus.choices}


def _parse_int(value: str | None, default: int, min_value: int = 0) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, parsed)


def _paginated_payload(request: HttpRequest, *, total: int, limit: int, offset: int, results: list[dict[str, Any]]) -> dict[str, Any]:
    query = request.GET.copy()

    def _url(new_offset: int) -> str:
        query["limit"] = str(limit)
        query["offset"] = str(new_offset)
        return f"{request.path}?{urlencode(query, doseq=True)}"

    next_url = _url(offset + limit) if offset + limit < total else None
    prev_offset = max(0, offset - limit)
    prev_url = _url(prev_offset) if offset > 0 else None
    return {"count": total, "next": next_url, "previous": prev_url, "results": results}


@login_required
@require_GET
def product_summary(request: HttpRequest) -> HttpResponse:
    projects = (
        get_authorized_aist_projects(Permissions.Product_View, user=request.user)
        .select_related("product")
        .prefetch_related("product__tags")
        .order_by("product__name")
    )

    product_ids = [project.product_id for project in projects]
    findings = get_authorized_findings(Permissions.Finding_View, user=request.user).filter(
        test__engagement__product_id__in=product_ids,
    )
    findings = findings.order_by()

    severity_annotations = {
        f"severity_{severity.lower()}": Count("id", filter=Q(severity=severity))
        for severity in API_SEVERITY_VALUES
    }
    counts = findings.values("test__engagement__product_id").annotate(
        total=Count("id"),
        active=Count("id", filter=Q(active=True)),
        risk_accepted=Count("id", filter=Q(risk_accepted=True)),
        under_review=Count("id", filter=Q(under_review=True)),
        mitigated=Count("id", filter=Q(is_mitigated=True)),
        **severity_annotations,
    )
    counts_by_product = {row["test__engagement__product_id"]: row for row in counts}

    latest_pipeline = AISTPipeline.objects.filter(project_id=OuterRef("id")).order_by("-updated", "-created")
    projects = projects.annotate(
        last_pipeline_id=Subquery(latest_pipeline.values("id")[:1]),
        last_pipeline_status=Subquery(latest_pipeline.values("status")[:1]),
        last_pipeline_updated=Subquery(
            latest_pipeline.values("updated")[:1],
            output_field=DateTimeField(),
        ),
    )

    results: list[dict[str, Any]] = []
    for project in projects:
        row = counts_by_product.get(project.product_id, {})
        severity = empty_severity_counts()
        for level in API_SEVERITY_VALUES:
            severity[level] = row.get(f"severity_{level.lower()}", 0)
        active_count = row.get("active", 0)
        last_pipeline_at = project.last_pipeline_updated or project.updated
        results.append(
            {
                "project_id": project.id,
                "product_id": project.product_id,
                "product_name": project.product.name,
                "tags": list(project.product.tags.all().values_list("name", flat=True)),
                "status": "active" if active_count else "inactive",
                "findings_total": row.get("total", 0),
                "findings_active": active_count,
                "severity": severity,
                "risk": {
                    "risk_accepted": row.get("risk_accepted", 0),
                    "under_review": row.get("under_review", 0),
                    "mitigated": row.get("mitigated", 0),
                },
                "last_pipeline": {
                    "id": project.last_pipeline_id,
                    "status": project.last_pipeline_status,
                    "updated": project.last_pipeline_updated,
                },
                "last_sync": last_pipeline_at,
            },
        )

    return JsonResponse({"results": results})


@login_required
@require_GET
def pipeline_summary(request: HttpRequest) -> HttpResponse:
    queryset = (
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
        .select_related("project", "project__product", "project_version", "project_version__resolved_from_branch")
        .order_by("-created")
    )

    project_id = request.GET.get("project_id")
    if project_id:
        try:
            queryset = queryset.filter(project_id=int(project_id))
        except ValueError:
            return JsonResponse({"project_id": ["Must be an integer."]}, status=400)

    status = request.GET.get("status")
    if status:
        if status not in PIPELINE_STATUS:
            return JsonResponse({"status": ["Invalid status value."]}, status=400)
        queryset = queryset.filter(status=status)

    created_gte = request.GET.get("created_gte")
    if created_gte:
        dt = parse_datetime(created_gte)
        if dt is None:
            return JsonResponse({"created_gte": ["Must be an ISO-8601 datetime."]}, status=400)
        queryset = queryset.filter(created__gte=dt)

    created_lte = request.GET.get("created_lte")
    if created_lte:
        dt = parse_datetime(created_lte)
        if dt is None:
            return JsonResponse({"created_lte": ["Must be an ISO-8601 datetime."]}, status=400)
        queryset = queryset.filter(created__lte=dt)

    search = (request.GET.get("search") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(project_version__version__icontains=search)
            | Q(project_version__resolved_from_branch__version__icontains=search),
        )

    ordering = request.GET.get("ordering")
    if ordering:
        if ordering not in PIPELINE_ORDERING:
            return JsonResponse({"ordering": ["Invalid ordering value."]}, status=400)
        queryset = queryset.order_by(ordering)

    queryset = queryset.distinct()
    total = queryset.count()
    limit = _parse_int(request.GET.get("limit"), default=50, min_value=1)
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)
    page = list(queryset[offset:offset + limit])

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
        refs = resolve_project_version_git_refs(pipeline.project_version)
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
                "branch": refs.branch,
                "commit": refs.commit,
                "findings": counts.get(pipeline.id, 0),
                "actions": actions,
            },
        )

    return JsonResponse(_paginated_payload(request, total=total, limit=limit, offset=offset, results=results))
