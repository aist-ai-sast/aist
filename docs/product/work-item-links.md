# Work-Item Links

A work-item link connects one AIST finding to a remediation ticket in
![](../assets/icons/jira.svg) Jira, ![](../assets/icons/youtrack.svg) YouTrack,
![](../assets/icons/github.svg) GitHub Issues, ![](../assets/icons/gitlab.svg) GitLab Issues,
![](../assets/icons/linear.svg) Linear, ![](../assets/icons/azure-devops.svg) Azure DevOps,
or to a manual URL.

![Work-item link status synchronisation](../assets/work-item-links.svg)

## Link model

A provider is an organization-owned connection to
![](../assets/icons/jira.svg) Jira, ![](../assets/icons/youtrack.svg) YouTrack,
![](../assets/icons/github.svg) GitHub, ![](../assets/icons/gitlab.svg) GitLab,
![](../assets/icons/linear.svg) Linear, ![](../assets/icons/azure-devops.svg) Azure DevOps,
or a Generic tracker. It holds its base URL, non-secret settings, active/sync
state, and encrypted token where required.

A provider-backed link stores the ticket identifier/key, URL, title, raw status,
normalized status category, last sync time, and error. A manual link stores a
URL only and is never fetched.

## Supported providers

- [Jira](../integrations/jira.md), [GitHub](../integrations/github.md), and
  [GitLab](../integrations/gitlab.md) have an implemented sync backend and
  refresh status automatically.
- [YouTrack](../integrations/youtrack.md), [Linear](../integrations/linear.md),
  and [Azure DevOps](../integrations/azure-devops.md) can be created and
  linked today, but have no sync backend yet — they behave like a manual link
  until one is implemented.

## Status refresh lifecycle

1. Creating a provider-backed link queues an immediate single-link refresh.
2. Celery Beat also schedules refreshes for active providers with sync enabled.
3. A worker fetches every provider ticket independently and updates its link.
4. It stores the current status, title, URL, timestamp, or `sync_error`.

Inactive providers, disabled sync, and provider types without a backend are
skipped. A failed ticket remains visible and never blocks its siblings.

## What the reviewer sees

Both the tracker’s raw status and its normalized category remain available on
the finding. They are engineering context only: ticket state cannot close a
finding or alter risk acceptance. If the provider requires it, worker requests
use its organization VPN route.

## Implementation references

- [Provider and link records](../../aist/models.py:1598)
- [Single-link and provider refresh](../../aist/work_items/sync.py:71)
