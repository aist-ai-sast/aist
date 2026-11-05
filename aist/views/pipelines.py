from __future__ import annotations

import uuid

import django_filters
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models, transaction
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from django.views.decorators.vary import vary_on_headers
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
from aist.execution.enqueue import LaunchEnqueueError, LaunchPrincipal, enqueue_pipeline_launch
from aist.forms import AISTPipelineRunForm, DastPipelineRunForm
from aist.launch_data import PipelineLaunchData
from aist.models import AISTLaunchConfigAction, AISTPipeline, AISTStatus, PipelineExecutionType
from aist.queries import (
    get_authorized_aist_pipelines,
    get_authorized_aist_projects,
    require_product_permission,
)
from aist.services.dast_queue import queue_is_addressable
from aist.services.pipeline_lifecycle import PipelineTransitionError, transition_allowed
from aist.tasks.ai import resolve_triage_type
from aist.utils.http import _qs_without, page_of
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
# Seconds between refreshes of the pipeline list: quick while a listed run can still change, slow otherwise.
PIPELINE_LIST_POLL_ACTIVE_SECONDS = 5
PIPELINE_LIST_POLL_IDLE_SECONDS = 30
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


class PipelineListStatusFilter(models.TextChoices):
    ALL = "ALL", "All statuses"
    FINISHED = "FINISHED", "Finished only (incl warnings)"


class PipelineHistoryFilter(django_filters.FilterSet):

    """The filters a pipeline history page is opened with; a value that does not parse is ignored."""

    project = django_filters.NumberFilter(field_name="project_id")
    q = django_filters.CharFilter(method="filter_text")
    status = django_filters.ChoiceFilter(choices=PipelineListStatusFilter.choices, method="filter_status")
    created_from = django_filters.DateFilter(field_name="created", lookup_expr="date__gte")
    created_to = django_filters.DateFilter(field_name="created", lookup_expr="date__lte")

    class Meta:
        model = AISTPipeline
        fields = ()

    def filter_text(self, queryset, name, value):
        return queryset.filter(Q(id__icontains=value) | Q(project__product__name__icontains=value))

    def filter_status(self, queryset, name, value):
        if value == PipelineListStatusFilter.FINISHED:
            return queryset.filter(status__in=get_terminal_pipeline_statuses())
        return queryset

    @property
    def selected(self) -> dict:
        """The valid filter values, for showing the form back as it was submitted."""
        self.is_valid()
        return self.form.cleaned_data


def _pipeline_history(request: HttpRequest, pipelines) -> PipelineHistoryFilter:
    return PipelineHistoryFilter(request.GET, queryset=pipelines.order_by("-updated"))


def _manual_launch_form(execution_type, data, *, projects, client_request_key):
    if execution_type == PipelineExecutionType.DAST:
        return DastPipelineRunForm(data, project_queryset=projects, client_request_key=client_request_key)
    form = AISTPipelineRunForm(data, client_request_key=client_request_key)
    form.fields["project"].queryset = projects
    return form


def _enqueue_manual_launch(request: HttpRequest, form, client_request_key: str) -> HttpResponse | None:
    """Queue the launch a valid start form describes; ``None`` with the refusal added to the form when it is refused."""
    project = form.cleaned_data["project"]
    require_product_permission(project.product, Permissions.Product_Edit, user=request.user)
    launch_data = PipelineLaunchData({})
    launch_data.one_off_actions = form.one_off_actions
    launch_data.one_off_actions_done = []
    try:
        launch_request = enqueue_pipeline_launch(
            arguments=form.get_arguments(),
            principal=LaunchPrincipal.for_user(organization=project.organization, requester=request.user),
            client_request_key=client_request_key,
            initial_launch_data=launch_data.as_dict(),
        ).request
    except LaunchEnqueueError as exc:
        form.add_error(None, str(exc))
        return None
    return redirect(f"{reverse('aist:launching_dashboard')}?queued_request={launch_request.pk}")


@login_required
def start_pipeline(request: HttpRequest) -> HttpResponse:
    """
    Present manual SAST/DAST forms and enqueue one durable launch request.

    Pipeline creation and Celery publication are owned by the generic
    dispatcher, so this user-facing view never bypasses queue policy.
    """
    client_request_key = (request.POST.get("client_request_key") or "").strip() or uuid.uuid4().hex
    requested_type = (request.POST.get("execution_type") or request.GET.get("execution_type") or "").upper()
    execution_type = (
        PipelineExecutionType.DAST if requested_type == PipelineExecutionType.DAST else PipelineExecutionType.SAST
    )
    authorized_projects = get_authorized_aist_projects(
        Permissions.Product_Edit,
        user=request.user,
    ).order_by("product__name")
    form = _manual_launch_form(
        execution_type,
        request.POST if request.method == "POST" else None,
        projects=authorized_projects,
        client_request_key=client_request_key,
    )
    if request.method == "POST" and form.is_valid():
        queued = _enqueue_manual_launch(request, form, client_request_key)
        if queued is not None:
            return queued

    history = _pipeline_history(
        request,
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
        .filter(status__in=get_terminal_pipeline_statuses())
        .select_related("project__product"),
    )
    add_breadcrumb(title="Start pipeline", top_level=True, request=request)
    return render(
        request,
        "aist/start.html",
        {
            "form": form,
            "history_page": page_of(
                request,
                history.qs,
                sizes=START_HISTORY_PAGE_SIZES,
                default=START_HISTORY_PAGE_SIZES[0],
            ),
            "selected_project": history.selected.get("project") or "",
            "search_query": history.selected.get("q") or "",
            "aist_status_choices": AISTStatus.choices,
            "aist_action_types": AISTLaunchConfigAction.ActionType.choices,
            "ai_filter_reference": get_ai_filter_reference(),
            "client_request_key": client_request_key,
            "execution_type": execution_type,
            "is_dast": execution_type == PipelineExecutionType.DAST,
            "history_projects": authorized_projects,
        },
    )


@login_required
@vary_on_headers("HX-Request")
def pipeline_list(request):
    history = _pipeline_history(
        request,
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user)
        .select_related("project__product", "project_version"),
    )
    page_obj = page_of(request, history.qs, sizes=PIPELINE_LIST_PAGE_SIZES, default=20)
    listed = list(page_obj.object_list)
    table_context = {
        "page_obj": page_obj,
        "pipelines": listed,
        "poll_seconds": (
            PIPELINE_LIST_POLL_ACTIVE_SECONDS
            if any(not is_terminal_pipeline_status(p.status) for p in listed)
            else PIPELINE_LIST_POLL_IDLE_SECONDS
        ),
    }
    if request.htmx:
        return render(request, "aist/_pipeline_list_table.html", table_context)
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
            **table_context,
            "selected_project": history.selected.get("project") or "",
            "search_query": history.selected.get("q") or "",
            "status": history.selected.get("status") or PipelineListStatusFilter.ALL,
            "status_choices": PipelineListStatusFilter.choices,
            "projects": projects,
            "page_sizes": PIPELINE_LIST_PAGE_SIZES,
        },
    )


@login_required
@vary_on_headers("HX-Request")
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
    if request.htmx:
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
    """POST-only: ask the pipeline's execution driver to stop it -- a cooperative stop request for DAST; task revoke, container cleanup and finish for the rest."""
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
