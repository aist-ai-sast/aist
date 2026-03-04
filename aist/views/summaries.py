from __future__ import annotations

import contextlib
import datetime
import statistics
from typing import Any
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Count, DateTimeField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, TruncWeek
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET
from dojo.authorization.roles_permissions import Permissions
from dojo.models import CWE, Finding

from aist.api.common import API_SEVERITY_VALUES, empty_severity_counts
from aist.models import AISTAIFindingResponse, AISTPipeline, AISTStatus
from aist.queries import get_authorized_aist_pipelines, get_authorized_aist_projects, get_authorized_findings
from aist.utils.cwe_lookup import fetch_cwe_meta, load_cwe_fixture_lookup, trim_text
from aist.utils.project_version_refs import resolve_project_version_git_refs

PIPELINE_ORDERING = {"created", "-created", "updated", "-updated"}
PIPELINE_STATUS = {status for status, _label in AISTStatus.choices}
TREND_WEEKS = 12
AGE_BUCKETS = ("0_7", "8_30", "31_90", "90_plus")
CWE_DISTRIBUTION_LIMIT = 12
CWE_META_CACHE_TIMEOUT_SECONDS = 60 * 60 * 24


def _build_cwe_distribution(findings_qs) -> list[dict[str, Any]]:
    rows = list(
        findings_qs.filter(active=True)
        .exclude(cwe__isnull=True)
        .exclude(cwe=0)
        .values("cwe")
        .annotate(total=Count("id"))
        .order_by("-total", "cwe")[:CWE_DISTRIBUTION_LIMIT],
    )
    if not rows:
        return []

    cwe_ids = [int(row["cwe"]) for row in rows if row.get("cwe")]
    fixture_lookup = load_cwe_fixture_lookup()
    cwe_lookup = {
        int(row["number"]): {"title": row.get("description", ""), "url": row.get("url", "")}
        for row in CWE.objects.filter(number__in=cwe_ids).values("number", "description", "url")
    }

    cache_keys = {cwe_id: f"aist:cwe:meta:{cwe_id}" for cwe_id in cwe_ids}
    cached_rows = cache.get_many(cache_keys.values())
    cached_meta: dict[int, dict[str, str] | None] = {
        cwe_id: meta if isinstance(meta, dict) else None
        for cwe_id, cache_key in cache_keys.items()
        for meta in [cached_rows.get(cache_key)]
    }
    result: list[dict[str, Any]] = []
    uncached_meta: dict[str, dict[str, str]] = {}
    for row in rows:
        cwe_id = int(row["cwe"])
        local = cwe_lookup.get(cwe_id) or fixture_lookup.get(cwe_id, {})
        meta = cached_meta.get(cwe_id) or fetch_cwe_meta(cwe_id)
        if not isinstance(meta, dict) or not meta:
            meta = {
                "title": trim_text(str(local.get("title", "")), max_length=160),
                "description": trim_text(str(local.get("title", "")), max_length=320),
                "impact": "",
                "url": str(local.get("url", "")),
            }
        if cached_meta.get(cwe_id) is None:
            uncached_meta[cache_keys[cwe_id]] = meta

        title = trim_text(str(meta.get("title") or local.get("title") or ""), max_length=160)
        result.append(
            {
                "cwe": cwe_id,
                "count": int(row.get("total", 0)),
                "title": title,
                "description": trim_text(str(meta.get("description", "")), max_length=320),
                "impact": trim_text(str(meta.get("impact", "")), max_length=320),
                "url": str(meta.get("url", "") or local.get("url", "")),
            },
        )
    if uncached_meta:
        cache.set_many(uncached_meta, timeout=CWE_META_CACHE_TIMEOUT_SECONDS)
    return result


def _series_start_week(now: datetime.datetime, *, weeks: int = TREND_WEEKS) -> datetime.datetime:
    start_of_week = now - datetime.timedelta(days=now.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_week - datetime.timedelta(weeks=weeks - 1)


def _week_key(dt: datetime.datetime) -> str:
    return dt.date().isoformat()


def _build_findings_aging_heatmap(findings_qs) -> dict[str, Any]:
    now = timezone.now()
    rows = findings_qs.filter(active=True).values("severity", "date", "created")
    matrix: dict[str, dict[str, int]] = {
        severity: dict.fromkeys(AGE_BUCKETS, 0)
        for severity in API_SEVERITY_VALUES
    }
    for row in rows:
        severity = row["severity"]
        created_at = row["date"] or row["created"]
        if severity not in matrix or not created_at:
            continue
        if isinstance(created_at, datetime.datetime):
            created_day = created_at.date()
        elif isinstance(created_at, datetime.date):
            created_day = created_at
        else:
            continue
        age_days = max((now.date() - created_day).days, 0)
        if age_days <= 7:
            bucket = "0_7"
        elif age_days <= 30:
            bucket = "8_30"
        elif age_days <= 90:
            bucket = "31_90"
        else:
            bucket = "90_plus"
        matrix[severity][bucket] += 1

    return {
        "buckets": list(AGE_BUCKETS),
        "severities": list(API_SEVERITY_VALUES),
        "matrix": matrix,
    }


def _build_risk_trend(findings_qs) -> list[dict[str, Any]]:
    now = timezone.now()
    start_week = _series_start_week(now)
    created_rows = (
        findings_qs.annotate(event_at=Coalesce("date", "created", output_field=DateTimeField()))
        .filter(event_at__gte=start_week)
        .annotate(week=TruncWeek("event_at"))
        .values("week")
        .annotate(total=Count("id"))
    )
    mitigated_rows = (
        findings_qs.filter(active=False)
        .exclude(last_status_update__isnull=True)
        .filter(last_status_update__gte=start_week)
        .annotate(week=TruncWeek("last_status_update"))
        .values("week")
        .annotate(total=Count("id"))
    )
    created_by_week = {_week_key(row["week"]): int(row["total"]) for row in created_rows if row["week"]}
    mitigated_by_week = {_week_key(row["week"]): int(row["total"]) for row in mitigated_rows if row["week"]}

    series: list[dict[str, Any]] = []
    for idx in range(TREND_WEEKS):
        week_dt = start_week + datetime.timedelta(weeks=idx)
        week = _week_key(week_dt)
        new_findings = created_by_week.get(week, 0)
        mitigated_findings = mitigated_by_week.get(week, 0)
        series.append(
            {
                "week": week,
                "new_findings": new_findings,
                "mitigated_findings": mitigated_findings,
                "net": new_findings - mitigated_findings,
            },
        )
    return series


def _build_pipeline_performance_trend(pipelines_qs) -> list[dict[str, Any]]:
    now = timezone.now()
    start_week = _series_start_week(now)
    rows = (
        pipelines_qs.filter(created__gte=start_week)
        .annotate(week=TruncWeek("created"))
        .values("week", "status", "created", "updated")
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        week_dt = row["week"]
        created = row["created"]
        updated = row["updated"]
        if not week_dt or not created or not updated:
            continue
        week = _week_key(week_dt)
        bucket = grouped.setdefault(week, {"durations": [], "runs": 0, "warnings": 0})
        duration_seconds = max(int((updated - created).total_seconds()), 0)
        bucket["durations"].append(duration_seconds)
        bucket["runs"] += 1
        if row["status"] == AISTStatus.FINISHED_WITH_WARNINGS:
            bucket["warnings"] += 1

    series: list[dict[str, Any]] = []
    for idx in range(TREND_WEEKS):
        week_dt = start_week + datetime.timedelta(weeks=idx)
        week = _week_key(week_dt)
        data = grouped.get(week, {"durations": [], "runs": 0, "warnings": 0})
        runs = int(data["runs"])
        durations = data["durations"]
        median_duration = int(statistics.median(durations)) if durations else 0
        warning_rate = (int(data["warnings"]) / runs) if runs else 0.0
        series.append(
            {
                "week": week,
                "runs": runs,
                "median_duration_seconds": median_duration,
                "warnings_rate": warning_rate,
            },
        )
    return series


def _build_ai_verdict_analytics(*, pipelines_qs, product_ids: list[int]) -> dict[str, Any]:
    ai_qs = AISTAIFindingResponse.objects.filter(
        pipeline__in=pipelines_qs,
        finding__test__engagement__product_id__in=product_ids,
    )
    verdict_keys = [choice for choice, _label in AISTAIFindingResponse.Verdict.choices]
    verdict_counts = dict.fromkeys(verdict_keys, 0)
    for row in ai_qs.values("verdict").annotate(total=Count("id")):
        verdict = row["verdict"]
        if verdict in verdict_counts:
            verdict_counts[verdict] = int(row["total"])

    severity_by_verdict = {
        severity: dict.fromkeys(verdict_keys, 0)
        for severity in API_SEVERITY_VALUES
    }
    by_severity_rows = ai_qs.values("finding__severity", "verdict").annotate(total=Count("id"))
    for row in by_severity_rows:
        severity = row["finding__severity"]
        verdict = row["verdict"]
        if severity in severity_by_verdict and verdict in severity_by_verdict[severity]:
            severity_by_verdict[severity][verdict] = int(row["total"])

    uncertainty_buckets = {"low": 0, "medium": 0, "high": 0}
    for value in ai_qs.exclude(uncertainty_level__isnull=True).values_list("uncertainty_level", flat=True):
        if value is None:
            continue
        if value <= 0.33:
            uncertainty_buckets["low"] += 1
        elif value <= 0.66:
            uncertainty_buckets["medium"] += 1
        else:
            uncertainty_buckets["high"] += 1

    return {
        "total": int(ai_qs.count()),
        "verdict_counts": verdict_counts,
        "severity_by_verdict": severity_by_verdict,
        "uncertainty_buckets": uncertainty_buckets,
    }


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


@login_required
@require_GET
def dashboard_summary(request: HttpRequest) -> HttpResponse:
    project_id: int | None = None
    with contextlib.suppress(ValueError, TypeError):
        project_id_raw = request.GET.get("project_id")
        if project_id_raw is not None:
            project_id = int(project_id_raw)

    projects = (
        get_authorized_aist_projects(Permissions.Product_View, user=request.user)
        .select_related("product")
    )
    if project_id is not None:
        projects = projects.filter(id=project_id)

    projects_list = list(projects)
    product_ids = [p.product_id for p in projects_list]
    product_id_to_project = {p.product_id: p for p in projects_list}

    findings_qs = (
        get_authorized_findings(Permissions.Finding_View, user=request.user)
        .filter(test__engagement__product_id__in=product_ids)
        .order_by()
    )
    pipelines_qs = get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
    if project_id is not None:
        pipelines_qs = pipelines_qs.filter(project_id=project_id)
    elif projects_list:
        pipelines_qs = pipelines_qs.filter(project_id__in=[project.id for project in projects_list])
    else:
        pipelines_qs = pipelines_qs.none()

    kpi = findings_qs.aggregate(
        total_active=Count("id", filter=Q(active=True)),
        critical_high=Count("id", filter=Q(active=True, severity__in=["Critical", "High"])),
        total_findings=Count("id"),
        risk_accepted=Count("id", filter=Q(risk_accepted=True)),
        false_p=Count("id", filter=Q(false_p=True)),
        out_of_scope=Count("id", filter=Q(out_of_scope=True)),
        mitigated=Count("id", filter=Q(is_mitigated=True)),
        under_review=Count("id", filter=Q(under_review=True)),
    )

    severity_annotations = {
        f"sev_{s.lower()}": Count("id", filter=Q(severity=s))
        for s in API_SEVERITY_VALUES
    }
    per_product = list(
        findings_qs.filter(active=True)
        .values("test__engagement__product_id", "test__engagement__product__name")
        .annotate(total=Count("id"), **severity_annotations)
        .order_by("-sev_critical", "-sev_high", "-total"),
    )

    severity_distribution = {
        s: sum(row.get(f"sev_{s.lower()}", 0) for row in per_product)
        for s in API_SEVERITY_VALUES
    }

    top_projects = []
    for row in per_product[:8]:
        pid = row["test__engagement__product_id"]
        project = product_id_to_project.get(pid)
        top_projects.append(
            {
                "project_id": project.id if project else None,
                "name": row.get("test__engagement__product__name", ""),
                "critical": row.get("sev_critical", 0),
                "high": row.get("sev_high", 0),
                "medium": row.get("sev_medium", 0),
                "low": row.get("sev_low", 0),
                "info": row.get("sev_info", 0),
                "total_active": row.get("total", 0),
            },
        )

    findings_aging_heatmap = _build_findings_aging_heatmap(findings_qs)
    risk_trend = _build_risk_trend(findings_qs)
    pipeline_performance_trend = _build_pipeline_performance_trend(pipelines_qs)
    cwe_distribution = _build_cwe_distribution(findings_qs)
    ai_verdict_analytics = _build_ai_verdict_analytics(pipelines_qs=pipelines_qs, product_ids=product_ids)

    return JsonResponse(
        {
            "kpi": {
                "total_active": kpi["total_active"],
                "critical_high": kpi["critical_high"],
                "total_findings": kpi["total_findings"],
                "risk_accepted": kpi["risk_accepted"],
                "projects_count": len(projects_list),
            },
            "severity_distribution": severity_distribution,
            "top_projects": top_projects,
            "finding_status_breakdown": {
                "active": kpi["total_active"],
                "mitigated": kpi["mitigated"],
                "risk_accepted": kpi["risk_accepted"],
                "under_review": kpi["under_review"],
                "false_positive": kpi["false_p"],
                "out_of_scope": kpi["out_of_scope"],
            },
            "findings_aging_heatmap": findings_aging_heatmap,
            "risk_trend": risk_trend,
            "pipeline_performance_trend": pipeline_performance_trend,
            "cwe_distribution": cwe_distribution,
            "ai_verdict_analytics": ai_verdict_analytics,
        },
    )
