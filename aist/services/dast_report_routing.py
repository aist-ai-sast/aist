"""Resolve report destinations once for both preview and finalization, without writes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aist.authz import Action, queryset_for_action
from aist.models import AISTProject, DastProjectBinding

if TYPE_CHECKING:
    from aist.integrations.dast_report import DastReportSection, ValidatedDastReport


@dataclass(frozen=True, slots=True)
class DastSectionRoute:
    section: DastReportSection
    binding: DastProjectBinding
    source_commit: str | None
    fallback_reason: str | None = None


def resolve_dast_report_routes(
    report: ValidatedDastReport,
    launch_binding: DastProjectBinding,
    *,
    bindings=None,
) -> tuple[DastSectionRoute, ...]:
    """Only an unambiguous enabled binding on this tenant's exact target can receive a section."""
    candidates = defaultdict(list)
    if bindings is None:
        bindings = DastProjectBinding.objects.filter(target_id=launch_binding.target_id).select_related(
            "project__product", "project__repository", "target__integration",
        )
    product_type_id = launch_binding.target.integration.organization.product_type_id
    for binding in bindings:
        if (
            binding.target_id == launch_binding.target_id
            and binding.enabled
            and binding.project.product.prod_type_id == product_type_id
        ):
            candidates[binding.source_repo_key].append(binding)
    routes = []
    for section in report.sections:
        destination = launch_binding
        reason = None
        if section.repository is not None:
            matches = candidates[section.repository]
            commit = report.source_commit_for(section.repository)
            if not isinstance(commit, str) or not commit.strip():
                reason = "missing_source_commit"
            elif not matches:
                reason = "no_enabled_binding"
            elif len(matches) != 1:
                reason = "ambiguous_binding"
            else:
                destination = matches[0]
        routes.append(DastSectionRoute(
            section=section,
            binding=destination,
            source_commit=report.source_commit_for(destination.source_repo_key),
            fallback_reason=reason,
        ))
    return tuple(routes)


def check_dast_route_permissions(
    routes: tuple[DastSectionRoute, ...],
    *,
    launch_binding: DastProjectBinding,
    user,
    action: Action,
) -> None:
    """Launch authority is checked by admission; cross-project destinations need their own authority."""
    project_ids = {route.binding.project_id for route in routes} - {launch_binding.project_id}
    if not project_ids:
        return
    allowed = set(queryset_for_action(resource=AISTProject, action=action, user=user).filter(
        pk__in=project_ids,
    ).values_list("pk", flat=True)) if user is not None else set()
    if project_ids != allowed:
        # Do not name a destination the caller may not even be allowed to read.
        msg = "The report routes to projects you are not authorized to access for this operation."
        raise PermissionError(msg)
