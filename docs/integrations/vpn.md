# VPN integration

A VPN integration lets AIST reach services that an organization exposes only
on a private network. It is an outbound route for selected integrations and
source operations; it does not place the AIST web application itself on the
client network.

The [VPN-routed operations](../data-flows/vpn-routed-operations.md) page shows
the two sidecar lifecycles and the runtime security boundary.

## Where the route is used

| Operation | Route selection |
|---|---|
| SCM validation, discovery, and import | VPN attached to the SCM integration, when configured |
| SAST and Claude-analysis source acquisition, builder access | VPN attached to the SCM integration that owns the repository, otherwise the project or organization VPN |
| Work-item status synchronization | VPN attached to the work-item provider |
| Interactive source-file fetch | Warm egress for the same VPN, resolved from the authorized project version |
| Deduplication backfill source read | The same warm egress, used only when it is already warm |
| DAST validation and execution | Only the VPN explicitly attached to the DAST integration |

AI-triage webhooks and Slack delivery do not currently use this route. Local AI
triage communicates with the local bridge rather than a client-owned system.

## Two sidecar lifecycles

Most worker operations use a short-lived sidecar. The worker starts it before
the outbound operation, passes either a proxy address or a shared container
network to the consumer, and removes it when the operation finishes.

Interactive source browsing uses warm egress because the web process neither
controls Docker nor waits for a VPN cold start. A worker starts or reuses one
warm sidecar for the selected VPN integration. A cold request returns a retry
response while warming is queued; idle sidecars are reaped and the pool is
bounded.

The two pools are independent. Browsing source files cannot reuse or block the
sidecar owned by a pipeline execution.

## Configure a VPN

1. In **Organization → Integrations**, create a VPN integration.
2. Upload the OpenVPN configuration and any credentials not embedded in it.
3. Save the integration and verify that it is active.
4. On the SCM integration, work-item provider, or DAST integration that needs
   private access, select this VPN.
5. Validate the consuming integration through the normal integration action.

The selected route must be an active VPN integration of the consumer's own
organization. AIST rejects any other choice when the consumer is saved, and it
checks the same rule again before using the route: a route that was disabled or
stopped being a VPN of that organization after it was selected is never used,
and a DAST integration whose route breaks the rule is not ready and does not run.
An SCM integration's route that breaks the rule is ignored, and the project or
organization VPN applies instead. A project can also resolve an organization VPN through
its integration configuration, but DAST deliberately does not use that
fallback: its private gateway requires the route attached directly to the DAST
integration.

## Credentials and failure behavior

VPN configuration and credential fields are encrypted at rest and are not
returned to ordinary readers. A worker decrypts them only when it starts a
sidecar. Container-runtime administrators remain privileged because the Docker
daemon receives the runtime configuration.

When a VPN is optional and no active route is selected, the operation proceeds
directly. When a private destination requires the selected route, missing or
invalid VPN configuration fails the operation instead of silently bypassing the
route.

## Operational limits

- A VPN route controls reachability, not AIST authorization. Organization and
  project permissions are evaluated independently.
- The warm egress pool is intended for interactive source-file access, not as a
  general organization proxy.
- GitHub App source access does not currently support a VPN-routed GitHub
  Enterprise Server connection. GitHub Issues synchronization can use a VPN
  through its work-item provider.

Use [tenant isolation and access](../security/tenant-isolation-and-access.md)
for ownership rules and [runtime deployment](../architecture/runtime-deployment.md)
for the worker and Docker boundaries.
