# Slack integration

Slack is a pipeline **action handler**: it posts a message (and optionally an
AI-results CSV) to one or more channels when a pipeline's status matches a
launch-config action trigger. See
[pipeline actions](../product/pipeline-actions.md) for the trigger/action
model shared by Slack, email, and write-log — this page covers only the
Slack-specific credential and delivery detail.

## Credentials

Slack uses a **bot token** (Slack Web API, not an incoming webhook URL):

- **Token** — a Slack bot token (`xoxb-...`) with permission to post messages
  and upload files. Stored encrypted in `OrgIntegration.secret` (type
  `SLACK`). There is no `config` field Slack requires — unlike Gerrit/Gitea,
  no serializer validator enforces anything beyond the secret being present
  when the integration is tested.
- Manage it on the `/integrations` page (React `OrgIntegrationsPage`) — type
  **Slack**. The "validate" action calls Slack's `auth.test` endpoint
  (`AISTSlackNotificationManager.test_token`) to confirm the token is live.

**Token resolution order** when an action actually fires
(`SlackAction._get_token` in `aist/actions.py`): the org's active `SLACK`
integration for the pipeline's project (via `resolve_integration`) first; if
none is configured, it falls back to the DefectDojo system-wide Slack token
(`system_settings.slack_token`). A project with no dedicated Slack
integration still gets notified if that system-wide token is set.

Slack delivery is **not VPN-routed** — unlike SCM discovery, work-item sync,
and pipeline source acquisition, `aist/notifications.py` calls the Slack Web
API directly with no `scoped_session`/sidecar involved.

## Message and trigger behaviour

The channel list, title, and description all come from the **action's own
config** (`AISTLaunchConfigAction.config`), not from the org integration:

- `channels` — one or more Slack channel names/IDs. No channels configured
  means the action is a silent no-op.
- `title` — defaults to `AIST [<project>] pipeline <id> status <new_status>`.
- `description` — defaults to a short status/branch/commit/findings-link
  message, or a fuller summary (severity breakdown, duration, false-positive
  count) when `include_common_summary` is set.
- `include_ai_csv` — attaches the latest AI response as a CSV file
  (`files.getUploadURLExternal` upload flow). Mutually exclusive with
  `include_common_summary`; setting both raises an error.

Delivery uses the Slack Web API directly: `chat.postMessage` for a plain
message, or the file-upload flow when a CSV is attached. A per-channel send
failure is logged and other channels still receive their message; if any
channel failed, the action raises after all channels have been attempted, so
the failure is recorded on the pipeline (see
[pipeline actions](../product/pipeline-actions.md)) without blocking
delivery to the other channels.

## Setup flow

1. Create a Slack app with a bot token scoped for `chat:write` (and file
   upload if `include_ai_csv` will be used), and invite the bot to the target
   channel(s).
2. `/integrations` → create a **Slack** `OrgIntegration` with that bot token
   as the secret, or rely on the system-wide Slack token if one is already
   configured for the deployment.
3. On a launch configuration, add a `PUSH_TO_SLACK` action with the target
   `channels` and the trigger status; optionally set `title`, `description`,
   `include_common_summary`, or `include_ai_csv`.
