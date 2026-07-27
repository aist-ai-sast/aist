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
| SAST source acquisition and builder access | VPN resolved for the project and selected source integration |
| Work-item status synchronization | VPN attached to the work-item provider |
| Interactive source-file fetch | Warm egress for the VPN resolved from the authorized project version |
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

The VPN and its consumer must belong to the same organization. A project can
also resolve an organization VPN through its integration configuration, but
DAST deliberately does not use that fallback: its private gateway requires the
route attached directly to the DAST integration.

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
