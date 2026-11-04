from __future__ import annotations

import json
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding
from dojo.utils import add_breadcrumb

from aist.ai_filter import (
    AIFilterError,
    apply_ai_filter,
    get_ai_filter_reference,
    select_by_ai_filter,
    severity_ordering,
    validate_and_normalize_filter,
)
from aist.api.launch_configs import ACTION_CREATE_SERIALIZERS
from aist.execution.enqueue import LaunchEnqueueError, LaunchPrincipal, enqueue_pipeline_launch
from aist.forms import AISTPipelineRunForm, DastPipelineRunForm
from aist.launch_data import PipelineLaunchData
from aist.models import AISTLaunchConfigAction, AISTPipeline, AISTStatus, PipelineExecutionType
from aist.pipeline_args import PipelineArguments
from aist.queries import (
    get_authorized_aist_pipelines,
    get_authorized_aist_projects,
    require_product_permission,
)
from aist.services.dast_queue import queue_is_addressable
from aist.services.pipeline_lifecycle import PipelineTransitionError, transition_allowed
from aist.tasks.ai import resolve_triage_type
from aist.utils.http import _fmt_duration, _qs_without, page_of
from aist.utils.pipeline import (
    get_terminal_pipeline_statuses,
    is_terminal_pipeline_status,
    set_pipeline_status,
    stop_pipeline,
)
from aist.views._common import ERR_PIPELINE_NOT_FOUND

FINDINGS_PAGE_SIZES = [25, 50, 100, 200]
START_HISTORY_PAGE_SIZES = [8]
PIPELINE_LIST_PAGE_SIZES = [10, 20, 50, 100]
FINDINGS_SEVERITY_BADGES = {
    "Critical": "danger",
    "High": "danger",
    "Medium": "warning",
    "Low": "success",
    "Info": "secondary",
}
# Each sort key with its ordering per direction; keys with a label are the table's sortable column headers.
FINDINGS_SORT_OPTIONS = [
    {
        "key": "severity",
        "label": "Severity",
        "width": "120px",
        "order": {
            "desc": severity_ordering(most_severe_first=True),
            "asc": severity_ordering(most_severe_first=False),
        },
    },
    {"key": "title", "label": "Finding", "order": {"desc": "-title", "asc": "title"}},
    {"key": "file", "label": "Location", "order": {"desc": "-file_path", "asc": "file_path"}},
    {"key": "cwe", "label": "CWE", "width": "90px", "order": {"desc": "-cwe", "asc": "cwe"}},
    {
        "key": "analyzer",
        "label": "Analyzer",
        "width": "140px",
        "order": {"desc": "-test__test_type__name", "asc": "test__test_type__name"},
    },
    {"key": "date", "order": {"desc": "-date", "asc": "date"}},
]
_FINDINGS_ORDERINGS = {option["key"]: option["order"] for option in FINDINGS_SORT_OPTIONS}


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


def _build_findings_context(request: HttpRequest, pipeline: AISTPipeline) -> dict:
    base_context = {
        "findings_total": 0,
        "findings_filtered_total": 0,
        "findings_showing_total": 0,
        "findings_page": None,
        "findings_rows": [],
        "findings_page_size": FINDINGS_PAGE_SIZES[0],
        "findings_page_sizes": FINDINGS_PAGE_SIZES,
        "findings_apply_ai_filter": False,
        "findings_ai_filter_available": False,
        "findings_ai_filter_summary": "",
        "findings_ai_filter_snapshot": None,
        "findings_ai_filter_error": "",
        "findings_qs_no_page": "",
        "findings_qs_no_ai": "",
        "findings_qs_no_sort": "",
        "findings_sort": "date",
        "findings_dir": "desc",
        "findings_sort_options": FINDINGS_SORT_OPTIONS,
        "findings_limit": None,
    }

    if not is_terminal_pipeline_status(pipeline.status):
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

    ai_filter_snapshot = PipelineLaunchData(pipeline.launch_data).ai.filter_snapshot
    ai_filter_available = bool(ai_filter_snapshot)

    apply_ai_filter_flag = ai_filter_available and _parse_bool_param(request.GET.get("apply_ai_filter"))
    ai_filter_error = ""
    qs, filtered_total, showing_total, limit = base_qs, base_total, base_total, None
    if apply_ai_filter_flag:
        try:
            qs, filtered_total, limit = _ai_filter_selection(base_qs, ai_filter_snapshot)
            showing_total = qs.count()
        except AIFilterError as exc:
            ai_filter_error = str(exc)
            apply_ai_filter_flag = False

    sort_key, direction, ordering = _findings_ordering(request)
    page_obj = page_of(
        request,
        qs.order_by(ordering, "-id"),
        sizes=FINDINGS_PAGE_SIZES,
        default=FINDINGS_PAGE_SIZES[0],
        prefix="findings_",
    )
    rows = [_finding_row(finding) for finding in page_obj.object_list]

    return {
        "findings_total": base_total,
        "findings_filtered_total": filtered_total,
        "findings_showing_total": showing_total,
        "findings_page": page_obj,
        "findings_rows": rows,
        "findings_page_size": page_obj.paginator.per_page,
        "findings_page_sizes": FINDINGS_PAGE_SIZES,
        "findings_apply_ai_filter": apply_ai_filter_flag,
        "findings_ai_filter_available": ai_filter_available,
        "findings_ai_filter_summary": _summarize_ai_filter(ai_filter_snapshot),
        "findings_ai_filter_snapshot": ai_filter_snapshot,
        "findings_ai_filter_error": ai_filter_error,
        "findings_qs_no_page": _qs_without(request, "findings_page"),
        "findings_qs_no_ai": _qs_without(request, "findings_page", "apply_ai_filter"),
        "findings_qs_no_sort": _qs_without(request, "findings_page", "findings_sort", "findings_dir"),
        "findings_sort": sort_key,
        "findings_dir": direction,
        "findings_sort_options": FINDINGS_SORT_OPTIONS,
        "findings_limit": limit,
    }


def _ai_filter_selection(base_qs, snapshot: dict) -> tuple:
    """Return the findings the launch's AI filter selects, how many matched its conditions, and its limit."""
    selected_ids = list(select_by_ai_filter(base_qs, snapshot).values_list("id", flat=True))
    matched_total = apply_ai_filter(base_qs, snapshot).count()
    return base_qs.filter(id__in=selected_ids), matched_total, validate_and_normalize_filter(snapshot)["limit"]


def _findings_ordering(request: HttpRequest) -> tuple[str, str, str]:
    """Return the requested sort key, its direction and the ordering expression; unknown values fall back."""
    sort_key = (request.GET.get("findings_sort") or "date").strip()
    if sort_key not in _FINDINGS_ORDERINGS:
        sort_key = "date"
    direction = (request.GET.get("findings_dir") or "desc").strip().lower()
    if direction not in {"asc", "desc"}:
        direction = "desc"
    return sort_key, direction, _FINDINGS_ORDERINGS[sort_key][direction]


def _finding_row(finding: Finding) -> dict:
    test_type = getattr(getattr(finding.test, "test_type", None), "name", None)
    file_label = finding.file_path or "-"
    if finding.line:
        file_label = f"{file_label}:{finding.line}" if finding.file_path else f"Line {finding.line}"
    return {
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
        require_product_permission(pipeline.project.product, Permissions.Product_Edit, user=request.user)
        try:
            set_pipeline_status(pipeline, AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI)
        except PipelineTransitionError as exc:
            messages.error(request, str(exc))
    return redirect("aist:pipeline_detail", pipeline_id=pipeline_id)


@login_required
def start_pipeline(request: HttpRequest) -> HttpResponse:
    """
    Present manual SAST/DAST forms and enqueue one durable launch request.

    Pipeline creation and Celery publication are owned by the generic
    dispatcher, so this user-facing view never bypasses queue policy.
    """
    project_id = request.GET.get("project")
    q = (request.GET.get("q") or "").strip()

    history_qs = (
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
        .filter(status__in=get_terminal_pipeline_statuses())
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

    page_obj = page_of(request, history_qs, sizes=START_HISTORY_PAGE_SIZES, default=START_HISTORY_PAGE_SIZES[0])

    history_items = [{
        "id": p.id,
        "project_name": getattr(getattr(p.project, "product", None), "name", str(p.project_id)),
        "updated": p.updated,
        "status": p.status,
        "execution_type": p.execution_type,
        "duration": _fmt_duration(p.created, p.updated),
    } for p in page_obj.object_list]

    history_qs_str = _qs_without(request, "page")
    add_breadcrumb(title="Start pipeline", top_level=True, request=request)
    client_request_key = (request.POST.get("client_request_key") or "").strip() or uuid.uuid4().hex
    execution_type = (request.POST.get("execution_type") or request.GET.get("execution_type") or "SAST").upper()
    if execution_type not in {PipelineExecutionType.SAST, PipelineExecutionType.DAST}:
        execution_type = PipelineExecutionType.SAST
    authorized_projects = get_authorized_aist_projects(
        Permissions.Product_Edit,
        user=request.user,
    ).order_by("product__name")

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
                "client_request_key": client_request_key,
                "execution_type": execution_type,
                "history_projects": authorized_projects,
            },
        )

    if request.method == "POST":
        if execution_type == PipelineExecutionType.DAST:
            form = DastPipelineRunForm(request.POST, project_queryset=authorized_projects)
        else:
            form = AISTPipelineRunForm(request.POST)
            form.fields["project"].queryset = authorized_projects
        if form.is_valid():
            project = form.cleaned_data["project"]
            arguments = (
                form.get_arguments()
                if execution_type == PipelineExecutionType.DAST
                else PipelineArguments.for_sast(project=project, raw_params=form.get_params())
            )

            raw_actions = request.POST.get("one_off_actions") or "[]"
            try:
                actions_payload = json.loads(raw_actions)
            except json.JSONDecodeError:
                form.add_error(None, "Invalid actions payload.")
                return render_start(form)

            if not isinstance(actions_payload, list):
                form.add_error(None, "Actions payload must be a list.")
                return render_start(form)

            if execution_type == PipelineExecutionType.DAST and actions_payload:
                form.add_error(None, "One-off actions are currently available only for SAST launches.")
                return render_start(form)

            one_off_actions = []
            for index, item in enumerate(actions_payload):
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
                action_id = item.get("id") or uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"aist-launch:{client_request_key}:{index}",
                ).hex
                one_off_actions.append({
                    "id": action_id,
                    "trigger_status": data["trigger_status"],
                    "action_type": data["action_type"],
                    "config": data.get("config") or {},
                })

            require_product_permission(
                project.product,
                Permissions.Product_Edit,
                user=request.user,
            )
            launch_data = PipelineLaunchData({})
            launch_data.one_off_actions = one_off_actions
            launch_data.one_off_actions_done = []
            try:
                launch_request = enqueue_pipeline_launch(
                    arguments=arguments,
                    principal=LaunchPrincipal.for_user(
                        organization=project.organization,
                        requester=request.user,
                    ),
                    client_request_key=client_request_key,
                    initial_launch_data=launch_data.as_dict(),
                ).request
            except LaunchEnqueueError as exc:
                form.add_error(None, str(exc))
                return render_start(form)
            destination = reverse("aist:launching_dashboard")
            return redirect(f"{destination}?queued_request={launch_request.pk}")
    elif execution_type == PipelineExecutionType.DAST:
        form = DastPipelineRunForm(project_queryset=authorized_projects)
    else:
        form = AISTPipelineRunForm()
        form.fields["project"].queryset = authorized_projects
    return render_start(form)


@login_required
def pipeline_list(request):
    project_id = request.GET.get("project")
    q = (request.GET.get("q") or "").strip()

    status = (request.GET.get("status") or "ALL").upper()  # ALL | FINISHED

    qs = (
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
        .select_related("project__product", "project_version")
        .order_by("-updated")
    )

    if status == "FINISHED":
        qs = qs.filter(status__in=get_terminal_pipeline_statuses())

    if project_id:
        qs = qs.filter(project_id=project_id)
    if q:
        qs = qs.filter(
            Q(id__icontains=q) |
            Q(project__product__name__icontains=q),
        )

    page_obj = page_of(request, qs, sizes=PIPELINE_LIST_PAGE_SIZES, default=20)

    items = [{
        "id": p.id,
        "project_name": getattr(getattr(p.project, "product", None), "name", str(p.project_id)),
        "project_version": getattr(p.project_version, "version", None),
        "created": p.created,
        "updated": p.updated,
        "status": p.status,
        "duration": _fmt_duration(p.created, p.updated),
        # Active = anything that is not FINISHED
        "is_active": not is_terminal_pipeline_status(p.status),
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
            "page_sizes": PIPELINE_LIST_PAGE_SIZES,
        },
    )


@login_required
def pipeline_detail(request, pipeline_id: str):
    """Display the status and logs for a pipeline. Adds actions (Stop/Delete) and connects SSE client to stream logs."""
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
        id=pipeline_id,
    )
    status_context = {
        "pipeline": pipeline,
        # The lifecycle forbids leaving a terminal status, so the action is offered only where it can succeed.
        "can_return_to_push_to_ai": transition_allowed(pipeline.status, AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI),
        "ai_auto_triage": PipelineLaunchData(pipeline.launch_data).ai.pushes_automatically(resolve_triage_type(pipeline)),
        **_build_findings_context(request, pipeline),
    }
    if request.headers.get("X-Partial") == "status":
        return render(request, "aist/_pipeline_status_container.html", status_context)

    add_breadcrumb(parent=pipeline, title="Pipeline Detail", top_level=False, request=request)
    return render(
        request,
        "aist/pipeline_detail.html",
        {
            **status_context,
            # Whether this pipeline names a DAST run at all. What the queue then shows -- including
            # whether the run is over and may no longer be commanded -- is the gateway's answer.
            "dast_queue_addressable": queue_is_addressable(pipeline),
        },
    )


@login_required
@require_http_methods(["POST"])
def stop_pipeline_view(request, pipeline_id: str):
    """POST-only endpoint to stop a running pipeline (Celery revoke). Sets FINISHED regardless of current state to keep UI consistent."""
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_Edit, user=request.user),
        id=pipeline_id,
    )
    require_product_permission(pipeline.project.product, Permissions.Product_Edit, user=request.user)
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
    require_product_permission(pipeline.project.product, Permissions.Product_Edit, user=request.user)
    if request.method == "POST":
        pipeline.delete()
        return redirect("aist:pipeline_list")
    add_breadcrumb(parent=pipeline, title="Delete pipeline", top_level=False, request=request)
    return render(request, "aist/confirm_delete.html", {"pipeline": pipeline})
