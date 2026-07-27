# Slack integration

Slack is a pipeline action handler. It posts a pipeline-status message and can
optionally attach an AI-results CSV when a configured pipeline status is
reached.

## Configure Slack access

1. Create a Slack app with a bot token.
2. Grant message-posting permission and file-upload permission when CSV
   delivery is required.
3. Invite the bot to each target channel.
4. In **Organization → Integrations**, create and validate a Slack integration
   with the bot token.

The organization token is stored encrypted. When an organization Slack
integration is unavailable, a deployment-wide Slack token may be used if the
operator configured one. Organizations that require strict credential
separation should configure their own integration and avoid relying on that
deployment fallback.

Slack delivery is direct and does not currently use an organization VPN.

## Configure a pipeline action

On the launch configuration, add a Slack action with:

- the pipeline status that triggers delivery;
- one or more channel names or IDs;
- an optional title and description;
- either a common pipeline summary or an AI-results CSV.

The common summary and AI CSV modes are mutually exclusive. An empty channel
list performs no delivery.

AIST attempts each configured channel independently. A failure in one channel
does not prevent attempts to the others, but the action is recorded as failed
when any requested delivery fails. See [pipeline actions](../product/pipeline-actions.md).
