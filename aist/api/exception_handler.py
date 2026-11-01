"""Strip DefectDojo's `pro` upsell from AIST API error responses."""
from __future__ import annotations

from dojo.api_v2.exception_handler import custom_exception_handler as dojo_exception_handler

_VENDOR_UPSELL_KEY = "pro"


def strip_vendor_upsell(data):
    """Remove the upsell key from a response body, leaving everything else untouched."""
    if isinstance(data, dict):
        data.pop(_VENDOR_UPSELL_KEY, None)
        return data
    if isinstance(data, list):
        for item in data:
            strip_vendor_upsell(item)
    return data


def aist_exception_handler(exc, context):
    """DRF ``EXCEPTION_HANDLER``: wraps the vendor handler, so its status-code mapping holds."""
    response = dojo_exception_handler(exc, context)
    if response is not None and getattr(response, "data", None) is not None:
        response.data = strip_vendor_upsell(response.data)
    return response
