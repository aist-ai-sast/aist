# Organization

An AIST organization represents one client. It is the tenant that owns that
client's users, projects, findings, integrations, work-item providers, and
configuration.

![What an AIST organization owns](../assets/organization.svg)

## Tenant contents

An organization owns its product type and AIST projects. Each project owns
source versions, pipeline runs, findings, launch configuration, and review
history. Organization configuration holds SCM, VPN, AI, DAST, Slack, and email
integrations, plus work-item providers. Credentials remain with the
organization configuration rather than being copied to projects.

## Users and projects

Users enter an organization through membership in its product type. Full
members receive their organization role across that client's projects;
restricted members receive only explicitly granted projects. Project-specific
rules can reduce or deny access, but cannot elevate the organization role.

Projects resolve organization configuration by integration type. A project can
select a particular integration, override non-secret settings, or disable that
type. See [tenant isolation and access](../security/tenant-isolation-and-access.md)
and [project and source onboarding](project-and-source-onboarding.md).
