"""Bounds on the admin pages whose work leaves this installation."""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps

from django.http import JsonResponse

from aist.api.throttling import SharedScopedRateThrottle

# One scope for all three providers: what a listing costs is the same whichever host
# answers, and a separate budget per provider would only multiply the bound.
SCM_PROJECT_LISTING_SCOPE = "aist_scm_project_listing"

TOO_MANY_LISTINGS = "Too many project listings for this organization. Try again shortly."


class OrganizationScopedRateThrottle(SharedScopedRateThrottle):

    """
    Counts per organization named in the request rather than per caller.

    What a listing costs -- a web worker held on a Celery result, and a call to that
    organization's own SCM host through the VPN sidecar -- falls on the organization
    however many operators ask for it, so they draw from one budget.
    """

    def get_cache_key(self, request, view):
        organization_id = (request.POST.get("organization_id") or "").strip()
        try:
            # The budget belongs to the organization, not to the spelling of its id: "5",
            # "05" and "+5" all resolve to one organization and reach one host, so they must
            # draw from one bucket. int() is also how the lookup downstream reads the field,
            # which keeps the two from disagreeing about which organization this is.
            organization = int(organization_id)
        except ValueError:
            # Not an organization at all. The view refuses it; until then it counts against
            # whoever sent it rather than against some organization's budget.
            return super().get_cache_key(request, view)
        return self.cache_format % {"scope": self.scope, "ident": f"organization-{organization}"}


@dataclass(frozen=True)
class _Scoped:

    """Carries the scope for a throttle that expects to be handed the view it guards."""

    throttle_scope: str


def throttled(scope: str, throttle_class=SharedScopedRateThrottle, message: str = "Too many requests."):
    """
    Apply a scoped rate throttle to a plain Django view.

    These pages are not DRF views, so they carry no ``throttle_classes`` and nothing
    applies one for them; the throttle itself needs only a scope to read its configured
    rate from. Placed below ``login_required``, so a request that is refused anyway
    never spends another caller's budget.
    """
    carrier = _Scoped(scope)

    def decorate(view):
        @wraps(view)
        def guarded(request, *args, **kwargs):
            throttle = throttle_class()
            if throttle.allow_request(request, carrier):
                return view(request, *args, **kwargs)
            response = JsonResponse({"ok": False, "error": message}, status=429)
            wait = throttle.wait()
            if wait:
                response["Retry-After"] = str(int(wait) + 1)
            return response

        return guarded

    return decorate
