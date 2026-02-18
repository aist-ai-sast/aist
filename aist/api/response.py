from __future__ import annotations

from typing import Any

from rest_framework.response import Response
from rest_framework.views import APIView

from aist.utils.secrets import mask_sensitive_data

_PATCHED_ATTR = "_aist_masked_response_patch_installed"
_ORIGINAL_FINALIZE_ATTR = "_aist_original_finalize_response"


class MaskedAPIResponse(Response):
    def __init__(self, data: Any = None, *args, **kwargs):
        super().__init__(mask_sensitive_data(data), *args, **kwargs)

    @classmethod
    def from_response(cls, response: Response) -> MaskedAPIResponse:
        return cls(
            data=response.data,
            status=response.status_code,
            template_name=response.template_name,
            headers=dict(response.headers),
            exception=response.exception,
            content_type=response.content_type,
        )


def install_masked_api_response() -> None:
    if getattr(APIView, _PATCHED_ATTR, False):
        return

    original_finalize = APIView.finalize_response

    def finalize_response(self, request, response, *args, **kwargs):
        if isinstance(response, Response) and not isinstance(response, MaskedAPIResponse):
            response = MaskedAPIResponse.from_response(response)
        return original_finalize(self, request, response, *args, **kwargs)

    setattr(APIView, _ORIGINAL_FINALIZE_ATTR, original_finalize)
    APIView.finalize_response = finalize_response
    setattr(APIView, _PATCHED_ATTR, True)
