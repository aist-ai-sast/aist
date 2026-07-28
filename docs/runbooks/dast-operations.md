# DAST operations

This runbook covers the AIST side of DAST onboarding, launch, cancellation,
manual import, and provider contract promotion. Mutating operations require
project-operate or organization-management permission as appropriate.

## Prerequisites

Obtain a provider-generated v2 onboarding bundle. For a private gateway, also
prepare an active VPN integration owned by the same organization. Do not copy
the service token or custom CA into launch parameters, environment files, or
logs.

## Onboard and synchronize

1. In **Organization → Integrations**, import the onboarding bundle.
2. Select the DAST VPN when the gateway is private.
3. Run integration validation.
4. Synchronize targets and capabilities.

Continue only when validation is **Ready**, synchronization succeeded, and at
least one target is available. A private gateway without an active credentialed
DAST VPN should remain not ready.

To edit an integration later, leave the bundle field empty when only the name or
the DAST VPN changes; the stored connection and token are kept. Changing the
gateway URL, CA, integrator identity, or fingerprint needs a bundle carrying the
matching token. A replaced connection or a changed route re-runs validation, so
wait for **Ready** again before relying on the integration.

API clients use the organization DAST import, integration validation,
capability synchronization, and target-list endpoints under `/api/v2/aist/`.

## Bind a target to a project

From the organization integration, select an available target and create a
binding for the AIST project. Review:

- the source-repository identity expected by the target;
- the current capability revision and schema;
- every required target parameter;
- whether autonomous execution is allowed.

The ready state should show no unresolved integration, capability, parameter,
source, or VPN issue. If the provider revision or schema changed, reload the
catalog and review the new snapshot instead of reusing stale defaults.

## Create and start a launch configuration

Create a DAST launch configuration from the enabled binding. Select the source
version and start it from the UI. API clients may enqueue the same configuration:

```text
POST projects/<project_id>/launch-configs/<config_id>/start/
```

A successful enqueue returns a launch-request identity. It does not guarantee
that a pipeline already exists. Observe the launch request until it contains a
pipeline identity, then follow that pipeline for execution progress and logs.

A readiness conflict is authoritative. Correct the integration, binding,
capability, source, parameter, or VPN condition rather than attempting to bypass
admission.

## Schedule or run once

Configure the schedule on the launch configuration with a five-field cron,
enabled state, and concurrency limit. **Run once** queues an immediate request
without changing the cron or recorded last tick.

Scheduled and immediate requests use the same authorization, readiness,
capacity, connector, and result path. See
[scheduled pipeline launches](../product/scheduled-pipeline-launches.md).

## Cancel and recover

Use the pipeline **Stop** action or:

```text
POST pipelines/<pipeline_id>/stop/
```

AIST persists cancellation intent before contacting DAST. Temporary provider
unreachability can leave the pipeline waiting for reconciliation. Do not delete
launch requests, leases, or pipeline rows to force completion; that removes the
durable state needed for safe retry and recovery.

When a run appears stuck, check in order:

1. the AIST pipeline and connector logs;
2. DAST integration readiness and VPN health;
3. the provider run identified by the AIST correlation value;
4. whether reconciliation later records a terminal provider outcome.

## Import an external result

In **Pipelines → Import**, select DAST, choose the enabled binding, upload the
complete provider terminal result, and review the validation preview before
confirming.

AIST derives the source revision from the binding. Do not supply a different
commit. Confirmation creates an import pipeline and repeats validation before
findings are persisted.

## Rotate or disable access

Generate a replacement onboarding bundle through the provider's rotation
operation, then rotate the integration token in AIST. Wait for the new
validation generation and capability synchronization before re-enabling
autonomous bindings.

Disable the integration when access must stop. Existing history remains, but
new autonomous launches should fail readiness.

## Promote a provider contract change

Refresh the reviewed provider contract snapshot from the AIST repository root:

```bash
./scripts/update-dast-contract.sh /path/to/reviewed/dast/source
```

Review the snapshot and provenance diff, run the compatibility checks, deploy
both reviewed sides to staging, and complete the
[DAST staging compatibility canary](dast-staging-canary.md) before production
promotion.

## Common failure conditions

| Symptom | Check |
|---|---|
| Integration not ready | Gateway URL policy, token, CA, explicit DAST VPN, and validation result |
| Validation reports a rejected endpoint | Absolute HTTPS URL with no credentials, query, or fragment, and port 443 or 8443 |
| Validation reports a TLS handshake failure | Gateway certificate chain, the CA carried in the bundle, and whether the gateway serves the hostname in the gateway URL. This is a certificate or naming fault, not a network one |
| Validation reports an unreachable gateway | DAST VPN health, gateway availability, and the listening port |
| Ready, but no targets to bind | Whether the gateway accepts a target-catalog request from this integrator identity. Reaching the gateway and being allowed to list its targets are separate permissions on the DAST side, so validation can succeed while the catalog is refused |
| Binding stale | Capability revision, schema digest, target availability, and parameter snapshot |
| Launch remains pending | Stored authority, readiness, capacity, and request expiry |
| Cancellation remains pending | Provider reachability and reconciliation progress |
| Import rejected | Complete terminal result, expected binding, source identity, size, and report format |
