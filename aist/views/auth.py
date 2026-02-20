from __future__ import annotations

from django.contrib.auth import logout
from django.shortcuts import redirect
from django.views.decorators.http import require_http_methods
from single_session.signals import remove_all_sessions


@require_http_methods(["POST"])
def logout_all_devices_view(request):
    if not request.user.is_authenticated:
        return redirect("client_login")

    user = request.user
    remove_all_sessions(sender=type(user), user=user, request=request)
    logout(request)
    return redirect("client_login")
