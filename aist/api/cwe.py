from __future__ import annotations

from django.core.cache import cache
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.schema import AISTApiTag
from aist.utils.cwe_lookup import fetch_cwe_meta

_CWE_CACHE_TIMEOUT = 60 * 60 * 24  # 24 hours


class AISTCweDetailAPI(APIView):

    """
    Return human-readable metadata for a CWE identifier.

    Data is sourced from the cwe2 library (full MITRE database) with fallback
    to the vendor DefectDojo fixture. Results are cached for 24 hours.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        operation_id="aist_cwe_detail",
        summary="Get CWE metadata by numeric ID",
        parameters=[OpenApiParameter(name="cwe_id", location="path", type=int)],
        responses={
            200: OpenApiResponse(description="CWE metadata: title, description, impact, url"),
            404: OpenApiResponse(description="CWE not found in database"),
        },
    )
    def get(self, request, cwe_id: int, *args, **kwargs):
        if cwe_id <= 0:
            return Response({"detail": "Invalid CWE ID."}, status=status.HTTP_400_BAD_REQUEST)

        cache_key = f"aist:cwe:meta:{cwe_id}"
        meta = cache.get(cache_key)
        if meta is None:
            meta = fetch_cwe_meta(cwe_id)
            if meta:
                cache.set(cache_key, meta, timeout=_CWE_CACHE_TIMEOUT)

        if not meta:
            return Response({"detail": "CWE not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(meta, status=status.HTTP_200_OK)
