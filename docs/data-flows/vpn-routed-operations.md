# VPN-routed operations

One organization VPN supports two independent runtime paths: a short-lived
sidecar for worker operations and warm egress for interactive source access.
The consumer selects the route from an authorized integration or project
version; callers do not supply a proxy or container name.

![VPN-routed operations](../assets/vpn-routed-operations.svg)

## Select the route

SCM operations and work-item synchronization use the VPN associated with the
integration that owns the outbound request. Pipeline source acquisition uses
the route resolved for the project and source integration. Interactive file
access resolves the same organization-owned route from the authorized project
version.

DAST uses a stricter boundary. Validation, capability synchronization, launch,
polling, result retrieval, cancellation, and recovery use only the active VPN
attached to the same-organization DAST integration. A private DAST gateway
without that route is rejected; AIST does not substitute a project or SCM VPN.

## Worker-operation sidecar

Before a routed worker operation begins, the worker starts a sidecar for that
operation. HTTP clients receive its proxy address. A SAST builder or standalone
connector can instead join the sidecar's network namespace. The sidecar is
removed when the operation exits, including handled failure paths.

If the operation does not require a VPN and no active route is selected, it
continues directly. If a selected private route is unavailable or lacks usable
credentials, the operation fails rather than bypassing the route.

## Warm egress for source files

The web process never creates a VPN container during a file request. It derives
the warm proxy address from the authorized project version. A worker starts or
reuses one warm sidecar for the selected VPN integration, and the findings UI
can request a best-effort prewarm before source snippets are shown.

When the proxy is cold, the file endpoint queues warming and asks the client to
retry. Idle sidecars are reaped and the pool has a maximum size. This lifecycle
is separate from execution sidecars so interactive browsing does not share an
execution container or lock.

## Security boundary

The organization owns the VPN configuration and every integration that may use
it. Cross-organization bindings are rejected or ignored during route
resolution. A worker decrypts the VPN material only to start the selected
sidecar; it is not carried in the business payload of the operation.

Docker control is a privileged host boundary because the daemon can inspect
container configuration and network state. The proxy is limited to configured
AIST container addresses and is not exposed as a public organization proxy.

A routed request names its destination to the proxy, and the proxy resolves that
name inside the tunnel. The reach of the route is therefore what bounds where a
routed connection can land; a name lookup performed outside the tunnel cannot
stand in for that boundary. Give a route no more reach than its consumers need.
[DAST integration](../integrations/dast.md#network-and-credentials) describes how
a DAST gateway destination is checked under this constraint.

See [VPN integration](../integrations/vpn.md) for configuration and
[tenant isolation and access](../security/tenant-isolation-and-access.md) for
the ownership model.
