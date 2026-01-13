from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from dojo.aist.forms import AISTPipelineRunForm
from dojo.aist.models import AISTPipeline, AISTProject, AISTStatus
from dojo.aist.tasks import run_sast_pipeline
from dojo.aist.utils.http import _fmt_duration, _qs_without
from dojo.aist.utils.pipeline import create_pipeline_object, stop_pipeline
from dojo.aist.views._common import ERR_PIPELINE_NOT_FOUND
from dojo.utils import add_breadcrumb


def pipeline_set_status(request, pipeline_id: str):
    if not AISTPipeline.objects.filter(id=pipeline_id).exists():
        raise Http404(ERR_PIPELINE_NOT_FOUND)

    if request.method == "POST":
        with transaction.atomic():
            pipeline = (
                AISTPipeline.objects
                .select_for_update()
                .get(id=pipeline_id)
            )
            pipeline.status = AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI
            pipeline.save(update_fields=["status", "updated"])
    return redirect("dojo_aist:pipeline_detail", pipeline_id=pipeline_id)


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
        AISTPipeline.objects
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

    if request.method == "POST":
        form = AISTPipelineRunForm(request.POST)
        if form.is_valid():
            params = form.get_params()

            with transaction.atomic():
                p = create_pipeline_object(
                    form.cleaned_data["project"],
                    form.cleaned_data.get("project_version")
                    or form.cleaned_data["project"].versions.order_by("-created").first(),
                    None,
                )
            # Launch the Celery task and record its id on the pipeline.
            async_result = run_sast_pipeline.delay(p.id, params)
            p.run_task_id = async_result.id
            p.save(update_fields=["run_task_id"])
            return redirect("dojo_aist:pipeline_detail", pipeline_id=p.id)
    else:
        form = AISTPipelineRunForm()
    return render(
        request,
        "dojo/aist/start.html",
        {
            "form": form,
            "history_page": page_obj,  # for pagination
            "history_items": history_items,
            "history_qs": history_qs_str,
            "selected_project": project_id or "",
            "search_query": q,
            "page_sizes": [10, 20, 50, 100],
        },
    )


def pipeline_list(request):
    project_id = request.GET.get("project")
    q = (request.GET.get("q") or "").strip()

    status = (request.GET.get("status") or "ALL").upper()  # ALL | FINISHED
    per_page = int(request.GET.get("page_size") or 20)

    qs = (
        AISTPipeline.objects
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

    projects = AISTProject.objects.select_related("product").order_by("product__name")
    add_breadcrumb(title="Pipeline History", top_level=True, request=request)
    return render(
        request,
        "dojo/aist/pipeline_list.html",
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


def pipeline_detail(request, pipeline_id: str):
    """Display the status and logs for a pipeline. Adds actions (Stop/Delete) and connects SSE client to stream logs."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    if request.headers.get("X-Partial") == "status":
        return render(request, "dojo/aist/_pipeline_status_container.html", {"pipeline": pipeline})

    add_breadcrumb(parent=pipeline, title="Pipeline Detail", top_level=False, request=request)
    return render(request, "dojo/aist/pipeline_detail.html", {"pipeline": pipeline})


@login_required
@require_http_methods(["POST"])
def stop_pipeline_view(request, pipeline_id: str):
    """POST-only endpoint to stop a running pipeline (Celery revoke). Sets FINISHED regardless of current state to keep UI consistent."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    stop_pipeline(pipeline)
    return redirect("dojo_aist:pipeline_detail", pipeline_id=pipeline.id)


@login_required
@require_http_methods(["GET", "POST"])
def delete_pipeline_view(request, pipeline_id: str):
    """Delete a pipeline after confirmation (POST). GET returns a confirm view."""
    pipeline = get_object_or_404(AISTPipeline, id=pipeline_id)
    if request.method == "POST":
        pipeline.delete()
        return redirect("dojo_aist:start_pipeline")
    add_breadcrumb(parent=pipeline, title="Delete pipeline", top_level=False, request=request)
    return render(request, "dojo/aist/confirm_delete.html", {"pipeline": pipeline})
