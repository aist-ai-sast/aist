# Jira integration

Jira has no SCM role in AIST — it is a **work-item provider**: an issue
tracker AIST can link findings to and optionally sync ticket status from.
Configured through the `WorkItemProvider` model (not `OrgIntegration`, which
is used for SCM/VPN/Slack/etc.). See
[work-item links](../product/work-item-links.md) for the product-level link
lifecycle (single-link refresh on creation, Celery Beat periodic refresh,
independent per-link failure). This page covers only the Jira-specific
operational detail.

## Credentials

Jira uses an **API token or personal access token**, via the `jira` Python
library (`jira==3.10.5`):

- **Base URL** — the Jira instance root, e.g. `https://company.atlassian.net`
  for Cloud, or a self-hosted Data Center URL. Stored in
  `WorkItemProvider.base_url`.
- **API token** — stored encrypted in `WorkItemProvider.api_token`.
  - **Jira Cloud**: an API token (`ATATT…`), used with **Basic auth**
    (email + token).
  - **Jira Data Center / Server**: a personal access token, used with
    **Bearer auth** (no email needed).
- **Account email** — `WorkItemProvider.provider_config["jira_email"]`.
  **Required for Jira Cloud** (Basic auth needs it); **omit for Data
  Center/Server**, which switches the backend to Bearer-token auth instead.
  There is no separate "instance type" field — the backend infers Cloud vs.
  Data Center purely from whether `jira_email` is set.

Credential validity is checked by calling `client.myself()`
(`JiraBackend.validate_credentials`).

## Ticket fields and status mapping

`JiraBackend.fetch_issue_status` looks up the ticket by
`link.external_id` or `link.external_key` (whichever is set) and requests
only the `summary` and `status` fields from the Jira REST API. It maps
Jira's `status.statusCategory.key` to AIST's normalized
`WorkItemStatusCategory`:

| Jira status category key | AIST status category |
|---|---|
| `new` | `OPEN` |
| `indeterminate` | `IN_PROGRESS` |
| `done` | `DONE` |
| anything else | `UNKNOWN` |

Jira's own status-category model has only these three categories, so
`CANCELLED` is never produced for a Jira-backed link. The stored title comes
from `summary`; the stored URL is built as `{base_url}/browse/{issue.key}`.

## VPN

Like every work-item backend, `WorkItemBackend.scoped_context` resolves
`WorkItemProvider.vpn_integration` (must belong to the same organization —
enforced the same way as SCM/work-item VPN bindings generally, see
[VPN integration](vpn.md)) and, if active, starts/reuses a VPN sidecar and
threads its proxy URL into the `jira` client for the duration of the sync
call. Without an active VPN, requests go directly to `base_url`.

## Setup flow

1. Create a `WorkItemProvider` (`provider_type=JIRA`, `base_url`,
   `api_token`, `provider_config.jira_email` for Cloud), optionally attaching
   a VPN integration for a Jira instance reachable only over VPN, and
   `sync_enabled=True` to allow periodic status refresh.
2. Link a finding to a Jira ticket (manually enter the ticket key/URL, or
   through the finding UI's create-link flow). Creating a provider-backed
   link queues an immediate single-link refresh
   (`aist.work_items.sync.sync_link`).
3. If `sync_enabled` is true, Celery Beat also periodically calls
   `sync_provider`, which re-fetches every link under that provider
   independently — one link's failure (stored in `WorkItemLink.sync_error`)
   never blocks the rest.
