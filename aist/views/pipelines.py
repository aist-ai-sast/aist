from __future__ import annotations

import json
import uuid

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from aist.ai_filter import apply_ai_filter, get_ai_filter_reference
from aist.api.launch_configs import ACTION_CREATE_SERIALIZERS
from aist.forms import AISTPipelineRunForm
from aist.models import AISTLaunchConfigAction, AISTPipeline, AISTStatus
from aist.queries import get_authorized_aist_pipelines, get_authorized_aist_projects
from aist.tasks import run_sast_pipeline
from aist.utils.action_config import encrypt_action_secret_config
from aist.utils.http import _fmt_duration, _qs_without
from aist.utils.pipeline import create_pipeline_object, set_pipeline_status, stop_pipeline
from aist.views._common import ERR_PIPELINE_NOT_FOUND
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding
from dojo.utils import add_breadcrumb

FINDINGS_PAGE_SIZES = [25, 50, 100, 200]
FINDINGS_SEVERITY_BADGES = {
    "Critical": "danger",
    "High": "danger",
    "Medium": "warning",
    "Low": "success",
    "Info": "secondary",
}
FINDINGS_SORT_OPTIONS = [
    {"key": "severity", "label": "Severity"},
    {"key": "date", "label": "Date"},
    {"key": "title", "label": "Title"},
    {"key": "cwe", "label": "CWE"},
    {"key": "file", "label": "File"},
    {"key": "analyzer", "label": "Analyzer"},
    {"key": "id", "label": "ID"},
]


def _parse_bool_param(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _summarize_ai_filter(snapshot: dict | None) -> str:
    if not snapshot:
        return ""
    parts = []
    limit = snapshot.get("limit")
    if isinstance(limit, int) and limit > 0:
        parts.append(f"limit {limit}")
    for key, conditions in snapshot.items():
        if key in {"limit", "order_by"}:
            continue
        if isinstance(conditions, list):
            values = []
            for cond in conditions:
                value = cond.get("value")
                if value is None:
                    continue
                values.append(str(value))
            if values:
                parts.append(f"{key}: {', '.join(values)}")
            else:
                parts.append(f"{key}: {len(conditions)}")
    return ", ".join(parts)


def _severity_rank_case():
    return Case(
        When(severity__iexact="Critical", then=Value(0)),
        When(severity__iexact="High", then=Value(1)),
        When(severity__iexact="Medium", then=Value(2)),
        When(severity__iexact="Low", then=Value(3)),
        When(severity__iexact="Informational", then=Value(4)),
        When(severity__iexact="Info", then=Value(4)),
        default=Value(9),
        output_field=IntegerField(),
    )


def _build_findings_context(request: HttpRequest, pipeline: AISTPipeline) -> dict:
    base_context = {
        "findings_total": 0,
        "findings_filtered_total": 0,
        "findings_showing_total": 0,
        "findings_page": None,
        "findings_rows": [],
        "findings_page_numbers": [],
        "findings_page_size": FINDINGS_PAGE_SIZES[0],
        "findings_page_sizes": FINDINGS_PAGE_SIZES,
        "findings_apply_ai_filter": False,
        "findings_ai_filter_available": False,
        "findings_ai_filter_summary": "",
        "findings_ai_filter_snapshot": None,
        "findings_ai_filter_pretty": "",
        "findings_ai_filter_error": "",
        "findings_qs_no_page": "",
        "findings_qs_no_ai": "",
        "findings_qs_no_sort": "",
        "findings_sort": "date",
        "findings_dir": "desc",
        "findings_sort_options": FINDINGS_SORT_OPTIONS,
        "findings_limit": None,
    }

    if pipeline.status != AISTStatus.FINISHED:
        return base_context

    tests_qs = pipeline.tests.all()
    if not tests_qs.exists():
        return base_context

    base_qs = (
        Finding.objects
        .filter(test__in=tests_qs)
        .select_related("test__test_type")
    )
    base_total = base_qs.count()

    launch_ai = (pipeline.launch_data or {}).get("ai") or {}
    ai_filter_snapshot = launch_ai.get("filter_snapshot")
    ai_filter_available = bool(ai_filter_snapshot)
    ai_filter_pretty = json.dumps(ai_filter_snapshot, indent=2, sort_keys=True) if ai_filter_snapshot else ""

    apply_ai_filter_flag = ai_filter_available and _parse_bool_param(request.GET.get("apply_ai_filter"))
    ai_filter_error = ""
    qs = base_qs
    if apply_ai_filter_flag:
        try:
            qs = apply_ai_filter(qs, ai_filter_snapshot)
        except ValueError as exc:
            ai_filter_error = str(exc)
            apply_ai_filter_flag = False
            qs = base_qs

    filtered_total = qs.count()

    raw_sort = (request.GET.get("findings_sort") or "date").strip()
    sort_keys = {opt["key"] for opt in FINDINGS_SORT_OPTIONS}
    sort_key = raw_sort if raw_sort in sort_keys else "date"

    direction = (request.GET.get("findings_dir") or "desc").strip().lower()
    if direction not in {"asc", "desc"}:
        direction = "desc"

    if sort_key == "severity":
        qs = qs.annotate(sev_rank=_severity_rank_case())
        order_expr = "-sev_rank" if direction == "asc" else "sev_rank"
    elif sort_key == "date":
        order_expr = "date" if direction == "asc" else "-date"
    elif sort_key == "title":
        order_expr = "title" if direction == "asc" else "-title"
    elif sort_key == "cwe":
        order_expr = "cwe" if direction == "asc" else "-cwe"
    elif sort_key == "file":
        order_expr = "file_path" if direction == "asc" else "-file_path"
    elif sort_key == "analyzer":
        order_expr = "test__test_type__name" if direction == "asc" else "-test__test_type__name"
    else:
        order_expr = "id" if direction == "asc" else "-id"

    qs = qs.order_by(order_expr, "-id")

    limit = None
    if apply_ai_filter_flag and isinstance(ai_filter_snapshot, dict):
        raw_limit = ai_filter_snapshot.get("limit")
        if raw_limit is not None:
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = None
    if limit and limit > 0:
        qs = list(qs[:limit])

    page_size = FINDINGS_PAGE_SIZES[0]
    raw_page_size = request.GET.get("findings_page_size")
    if raw_page_size:
        try:
            parsed = int(raw_page_size)
        except ValueError:
            parsed = page_size
        if parsed in FINDINGS_PAGE_SIZES:
            page_size = parsed

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("findings_page") or 1)

    rows = []
    for finding in page_obj.object_list:
        test_type = getattr(getattr(finding.test, "test_type", None), "name", None)
        file_label = finding.file_path or "-"
        if finding.line:
            file_label = f"{file_label}:{finding.line}" if finding.file_path else f"Line {finding.line}"
        rows.append(
            {
                "id": finding.id,
                "title": finding.title,
                "severity": finding.severity,
                "severity_class": FINDINGS_SEVERITY_BADGES.get(finding.severity, "secondary"),
                "file_label": file_label,
                "cwe": finding.cwe or "",
                "test_type": test_type or "-",
                "date": finding.date,
                "description": finding.description or "",
                "mitigation": finding.mitigation or "",
                "is_false_positive": bool(finding.false_p),
            },
        )

    def _page_window(page):
        if not page or page.paginator.num_pages <= 1:
            return []
        start = max(1, page.number - 2)
        end = min(page.paginator.num_pages, page.number + 2)
        return list(range(start, end + 1))

    showing_total = min(filtered_total, limit) if limit and limit > 0 else filtered_total

    return {
        "findings_total": base_total,
        "findings_filtered_total": filtered_total,
        "findings_showing_total": showing_total,
        "findings_page": page_obj,
        "findings_rows": rows,
        "findings_page_numbers": _page_window(page_obj),
        "findings_page_size": page_size,
        "findings_page_sizes": FINDINGS_PAGE_SIZES,
        "findings_apply_ai_filter": apply_ai_filter_flag,
        "findings_ai_filter_available": ai_filter_available,
        "findings_ai_filter_summary": _summarize_ai_filter(ai_filter_snapshot),
        "findings_ai_filter_snapshot": ai_filter_snapshot,
        "findings_ai_filter_pretty": ai_filter_pretty,
        "findings_ai_filter_error": ai_filter_error,
        "findings_qs_no_page": _qs_without(request, "findings_page"),
        "findings_qs_no_ai": _qs_without(request, "findings_page", "apply_ai_filter"),
        "findings_qs_no_sort": _qs_without(request, "findings_page", "findings_sort", "findings_dir"),
        "findings_sort": sort_key,
        "findings_dir": direction,
        "findings_sort_options": FINDINGS_SORT_OPTIONS,
        "findings_limit": limit if limit and limit > 0 else None,
    }


@login_required
def pipeline_set_status(request, pipeline_id: str):
    if not get_authorized_aist_pipelines(Permissions.Product_Edit, user=request.user).filter(id=pipeline_id).exists():
        raise Http404(ERR_PIPELINE_NOT_FOUND)

    with transaction.atomic():
        pipeline = (
            get_authorized_aist_pipelines(Permissions.Product_Edit, user=request.user)
            .select_for_update()
            .get(id=pipeline_id)
        )
        user_has_permission_or_403(request.user, pipeline.project.product, Permissions.Product_Edit)
        set_pipeline_status(pipeline, AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI)
    return redirect("aist:pipeline_detail", pipeline_id=pipeline_id)


@login_required
def start_pipeline(request: HttpRequest) -> HttpResponse:
    """
    Launch a new SAST pipeline or redirect to the active one.

    If there is an existing pipeline that hasn't finished yet the user
    is redirected to its detail page. Otherwise this view presents a
    form allowing the user to configure and start a new pipeline. On
    successful submission a new pipeline is created and the Celery
    task is triggered.
    """
    project_id = request.GET.get("project")
    q = (request.GET.get("q") or "").strip()

    history_qs = (
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
        .filter(status=AISTStatus.FINISHED)
        .select_related("project__product")
    )
    if project_id:
        history_qs = history_qs.filter(project_id=project_id)
    if q:
        history_qs = history_qs.filter(
            Q(id__icontains=q) |
            Q(project__product__name__icontains=q),
        )

    history_qs = history_qs.order_by("-updated")

    per_page = int(request.GET.get("page_size") or 8)
    paginator = Paginator(history_qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    history_items = [{
        "id": p.id,
        "project_name": getattr(getattr(p.project, "product", None), "name", str(p.project_id)),
        "updated": p.updated,
        "status": p.status,
        "duration": _fmt_duration(p.created, p.updated),
    } for p in page_obj.object_list]

    history_qs_str = _qs_without(request, "page")
    add_breadcrumb(title="Start pipeline", top_level=True, request=request)

    def render_start(form):
        return render(
            request,
            "aist/start.html",
            {
                "form": form,
                "history_page": page_obj,  # for pagination
                "history_items": history_items,
                "history_qs": history_qs_str,
                "selected_project": project_id or "",
                "search_query": q,
                "page_sizes": [10, 20, 50, 100],
                "aist_status_choices": AISTStatus.choices,
                "aist_action_types": AISTLaunchConfigAction.ActionType.choices,
                "ai_filter_reference": get_ai_filter_reference(),
            },
        )

    if request.method == "POST":
        form = AISTPipelineRunForm(request.POST)
        form.fields["project"].queryset = get_authorized_aist_projects(
            Permissions.Product_Edit,
            user=request.user,
        ).order_by("product__name")
        if form.is_valid():
            params = form.get_params()

            raw_actions = request.POST.get("one_off_actions") or "[]"
            try:
                actions_payload = json.loads(raw_actions)
            except json.JSONDecodeError:
                form.add_error(None, "Invalid actions payload.")
                return render_start(form)

            if not isinstance(actions_payload, list):
                form.add_error(None, "Actions payload must be a list.")
                return render_start(form)

            one_off_actions = []
            for item in actions_payload:
                if not isinstance(item, dict):
                    form.add_error(None, "Invalid action payload.")
                    return render_start(form)
                action_type = item.get("action_type")
                serializer_cls = ACTION_CREATE_SERIALIZERS.get(action_type)
                if not serializer_cls:
                    form.add_error(None, f"Unknown action type: {action_type}")
                    return render_start(form)

                serializer = serializer_cls(data=item)
                if not serializer.is_valid():
                    form.add_error(None, serializer.errors)
                    return render_start(form)

                data = serializer.validated_data
                action_id = item.get("id") or uuid.uuid4().hex
                one_off_actions.append({
                    "id": action_id,
                    "trigger_status": data["trigger_status"],
                    "action_type": data["action_type"],
                    "config": data.get("config") or {},
                    "secret_config": encrypt_action_secret_config(data.get("secret_config") or {}),
                })

            with transaction.atomic():
                user_has_permission_or_403(
                    request.user,
                    form.cleaned_data["project"].product,
                    Permissions.Product_Edit,
                )
                p = create_pipeline_object(
                    form.cleaned_data["project"],
                    form.cleaned_data.get("project_version")
                    or form.cleaned_data["project"].versions.order_by("-created").first(),
                    None,
                )
                if one_off_actions:
                    launch_data = p.launch_data or {}
                    launch_data["one_off_actions"] = one_off_actions
                    launch_data["one_off_actions_done"] = []
                    p.launch_data = launch_data
                    p.save(update_fields=["launch_data"])
            # Launch the Celery task and record its id on the pipeline.
            async_result = run_sast_pipeline.delay(p.id, params)
            p.run_task_id = async_result.id
            p.save(update_fields=["run_task_id"])
            return redirect("aist:pipeline_detail", pipeline_id=p.id)
    else:
        form = AISTPipelineRunForm()
        form.fields["project"].queryset = get_authorized_aist_projects(
            Permissions.Product_Edit,
            user=request.user,
        ).order_by("product__name")
    return render_start(form)


@login_required
def pipeline_list(request):
    project_id = request.GET.get("project")
    q = (request.GET.get("q") or "").strip()

    status = (request.GET.get("status") or "ALL").upper()  # ALL | FINISHED
    per_page = int(request.GET.get("page_size") or 20)

    qs = (
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
        .select_related("project__product", "project_version")
        .order_by("-updated")
    )

    if status == "FINISHED":
        qs = qs.filter(status=AISTStatus.FINISHED)

    if project_id:
        qs = qs.filter(project_id=project_id)
    if q:
        qs = qs.filter(
            Q(id__icontains=q) |
            Q(project__product__name__icontains=q),
        )

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    items = [{
        "id": p.id,
        "project_name": getattr(getattr(p.project, "product", None), "name", str(p.project_id)),
        "project_version": getattr(p.project_version, "version", None),
        "created": p.created,
        "updated": p.updated,
        "status": p.status,
        "duration": _fmt_duration(p.created, p.updated),
        # Active = anything that is not FINISHED
        "is_active": p.status != AISTStatus.FINISHED,
    } for p in page_obj.object_list]

    qs_str = _qs_without(request, "page")

    projects = (
        get_authorized_aist_projects(Permissions.Product_View, user=request.user)
        .select_related("product")
        .order_by("product__name")
    )
    add_breadcrumb(title="Pipeline History", top_level=True, request=request)
    return render(
        request,
        "aist/pipeline_list.html",
        {
            "page_obj": page_obj,
            "items": items,
            "qs": qs_str,
            "selected_project": project_id or "",
            "search_query": q,
            "status": status,
            "projects": projects,
        },
    )


@login_required
def pipeline_detail(request, pipeline_id: str):
    """Display the status and logs for a pipeline. Adds actions (Stop/Delete) and connects SSE client to stream logs."""
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
        id=pipeline_id,
    )
    findings_context = _build_findings_context(request, pipeline)
    if request.headers.get("X-Partial") == "status":
        return render(
            request,
            "aist/_pipeline_status_container.html",
            {"pipeline": pipeline, **findings_context},
        )

    add_breadcrumb(parent=pipeline, title="Pipeline Detail", top_level=False, request=request)
    return render(request, "aist/pipeline_detail.html", {"pipeline": pipeline, **findings_context})


@login_required
@require_http_methods(["POST"])
def stop_pipeline_view(request, pipeline_id: str):
    """POST-only endpoint to stop a running pipeline (Celery revoke). Sets FINISHED regardless of current state to keep UI consistent."""
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_Edit, user=request.user),
        id=pipeline_id,
    )
    user_has_permission_or_403(request.user, pipeline.project.product, Permissions.Product_Edit)
    stop_pipeline(pipeline)
    return redirect("aist:pipeline_detail", pipeline_id=pipeline.id)


@login_required
@require_http_methods(["GET", "POST"])
def delete_pipeline_view(request, pipeline_id: str):
    """Delete a pipeline after confirmation (POST). GET returns a confirm view."""
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_Edit, user=request.user),
        id=pipeline_id,
    )
    user_has_permission_or_403(request.user, pipeline.project.product, Permissions.Product_Edit)
    if request.method == "POST":
        pipeline.delete()
        return redirect("aist:pipeline_list")
    add_breadcrumb(parent=pipeline, title="Delete pipeline", top_level=False, request=request)
    return render(request, "aist/confirm_delete.html", {"pipeline": pipeline})
