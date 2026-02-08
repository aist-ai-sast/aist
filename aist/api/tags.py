from __future__ import annotations

from django.core.cache import cache
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from dojo.authorization.roles_permissions import Permissions
from dojo.finding.queries import get_authorized_findings


class AvailableFindingTagsAPI(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        product_id = request.query_params.get("product_id")
        cache_key = f"aist_findings_tags_{request.user.id}_{product_id or 'all'}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response({"tags": cached})

        findings = get_authorized_findings(Permissions.Finding_View, user=request.user)
        if product_id:
            findings = findings.filter(test__engagement__product=product_id)
        tags = (
            findings.values_list("tags__name", flat=True)
            .exclude(tags__name__isnull=True)
            .exclude(tags__name__exact="")
            .distinct()
            .order_by("tags__name")
        )
        result = list(tags)
        cache.set(cache_key, result, 300)
        return Response({"tags": result})
