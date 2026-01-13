from __future__ import annotations

import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

from django.conf import settings
from django.urls import reverse


def _best_effort_outbound_ip() -> str:
    """Return the outbound IP chosen by the OS when connecting to a public address (no packets actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _is_abs_url(value: str) -> bool:
    try:
        p = urlsplit(value)
        return bool(p.scheme and p.netloc)
    except Exception:
        return False


def _scheme_from_settings_or_request(request):
    """Decide scheme respecting SECURE_SSL_REDIRECT and reverse proxy headers."""
    if getattr(settings, "SECURE_SSL_REDIRECT", False):
        return "https"

    hdr = getattr(settings, "SECURE_PROXY_SSL_HEADER", None)
    if request is not None and hdr:
        header_name, expected_value = hdr
        actual = request.META.get(header_name, "")
        if actual.split(",")[0].strip().lower() == expected_value.lower():
            return "https"

    if request is not None and request.is_secure():
        return "https"

    return "http"


def _normalize_base_url(url: str) -> str:
    """Return 'scheme://host[:port]' without path/query/fragment and no trailing slash."""
    p = urlsplit(url)
    scheme = p.scheme or "http"
    netloc = p.netloc or p.path
    if not netloc:
        return ""
    return urlunsplit((scheme, netloc.strip("/"), "", "", "")).rstrip("/")


def get_public_base_url() -> str:
    return getattr(settings, "PUBLIC_BASE_URL", "https://157.90.113.55:8443/")


def build_callback_url(pipeline_id: str) -> str:
    base = get_public_base_url()
    path = reverse("dojo_aist:pipeline_callback", kwargs={"pipeline_id": str(pipeline_id)})
    return urljoin(base + "/", path.lstrip("/"))
