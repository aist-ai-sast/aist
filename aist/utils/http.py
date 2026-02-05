from __future__ import annotations

from urllib.parse import urlencode


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
