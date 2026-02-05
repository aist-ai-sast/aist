from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods

from aist.api.pipelines import export_ai_results_response
from aist.models import AISTPipeline
from aist.queries import get_authorized_aist_pipelines
from dojo.authorization.roles_permissions import Permissions


@login_required
@require_http_methods(["POST"])
def export_ai_results(request: HttpRequest, pipeline_id: str) -> HttpResponse:
    """
    Export the latest AI response for a pipeline as a tabular file.

    All heavy lifting (parsing AI payload, sorting by impactScore, filtering
    false positives and limiting the number of findings) happens on the backend.
    """
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
        id=pipeline_id,
    )

    return export_ai_results_response(request, pipeline)
