from __future__ import annotations

from typing import Any

from rest_framework.response import Response
from rest_framework.views import APIView

from aist.utils.secrets import mask_sensitive_data

_PATCHED_ATTR = "_aist_masked_response_patch_installed"
_ORIGINAL_FINALIZE_ATTR = "_aist_original_finalize_response"
_MASKED_API_PATH_PREFIXES = (
    "/api/v2/aist/",
    "/aist-admin/api/v2/aist/",
    "/projects_version/",
)


class MaskedAPIResponse(Response):
    @staticmethod
    def _is_openapi_payload(data: Any) -> bool:
        return isinstance(data, dict) and "openapi" in data and "paths" in data and "info" in data

    def __init__(self, data: Any = None, *args, skip_mask: bool = False, **kwargs):
        payload = data if skip_mask or self._is_openapi_payload(data) else mask_sensitive_data(data)
        super().__init__(payload, *args, **kwargs)

    @classmethod
    def from_response(cls, response: Response) -> MaskedAPIResponse:
        content_type = (response.content_type or "").lower()
        skip_mask = "application/vnd.oai.openapi+json" in content_type
        return cls(
            data=response.data,
            status=response.status_code,
            template_name=response.template_name,
            headers=dict(response.headers),
            exception=response.exception,
            content_type=response.content_type,
            skip_mask=skip_mask,
        )


def install_masked_api_response() -> None:
    if getattr(APIView, _PATCHED_ATTR, False):
        return

    original_finalize = APIView.finalize_response

    def finalize_response(self, request, response, *args, **kwargs):
        path = getattr(request, "path_info", "") or getattr(request, "path", "")
        if (
            isinstance(response, Response)
            and not isinstance(response, MaskedAPIResponse)
            and path.startswith(_MASKED_API_PATH_PREFIXES)
        ):
            response = MaskedAPIResponse.from_response(response)
        return original_finalize(self, request, response, *args, **kwargs)

    setattr(APIView, _ORIGINAL_FINALIZE_ATTR, original_finalize)
    APIView.finalize_response = finalize_response
    setattr(APIView, _PATCHED_ATTR, True)
