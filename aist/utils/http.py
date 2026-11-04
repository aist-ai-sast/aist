from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlencode

from django.core.paginator import Page, Paginator

if TYPE_CHECKING:
    from collections.abc import Sequence


def _fmt_duration(start, end):
    if not start or not end:
        return None
    total = int((end - start).total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _qs_without(request, *keys):
    params = request.GET.copy()
    for k in keys:
        params.pop(k, None)
    return urlencode(params, doseq=True)


def page_of(request, items, *, sizes: Sequence[int], default: int, prefix: str = "") -> Page:
    """The page of ``items`` the request asks for; a page size outside ``sizes`` reads as ``default``."""
    try:
        size = int(request.GET.get(f"{prefix}page_size") or default)
    except ValueError:
        size = default
    if size not in sizes:
        size = default
    return Paginator(items, size).get_page(request.GET.get(f"{prefix}page") or 1)
