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

- the source-repository identity expected by the target, when it declares a
  repository-trigger requirement; a target with no such requirement (a
  fixed-surface or declared-stand scenario) is bound without one, and the
  binding rejects a source identity if one is supplied;
- the current capability revision and schema;
- every required target parameter;
- whether the binding is enabled — a disabled binding neither launches nor
  accepts an imported result.

The ready state should show no unresolved integration, capability, parameter,
source, or VPN issue. If the provider revision or schema changed, reload the
catalog and review the new snapshot instead of reusing stale defaults.

## Start once or create a launch configuration

For a single run, open **Start pipeline**, select **DAST**, then choose the
project, enabled binding, and target parameters, plus an explicit Git source
version when the binding's target requires a repository trigger. This path
creates a durable request but does not save a preset.

For reuse or scheduling, create a DAST launch configuration in the launch
dashboard. The preset stores its binding and schema-validated parameters, and
a Git trigger only when the binding's target requires one. Start it from the
dashboard or enqueue it through the API:

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

Configure the schedule on the launch configuration with a five-field cron and
enabled state. DAST capacity is one integration slot and is not operator
configurable. **Run once** queues an immediate request without changing the cron
or recorded last tick.

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

### What ends a run

A long scan is not a stuck one. Two independent limits end an autonomous run, and
both are set from the environment on the Celery worker and beat services:

| Setting | Default | What it bounds |
|---|---|---|
| `AIST_DAST_PROVIDER_STALL_TIMEOUT_SECONDS` | 1 hour | How long the provider may deliver nothing — no run identity, no new log output — before the run is given up. This is what ends a run in practice, and what stops an unreachable provider from being retried indefinitely. `0` removes it. |
| `AIST_DAST_EXECUTION_TIMEOUT_SECONDS` | 7 days | The ceiling on total run length. A safety limit, not a scan budget; a healthy scan does not reach it. `0` removes it. |
| `AIST_DAST_UNREACHABLE_GRACE_SECONDS` | 1 hour | How long a run past that ceiling is still retried before it is abandoned. |

Removing both bounds leaves nothing that ends a run whose provider has gone away,
so remove at most one. Raising the ceiling is the safe adjustment; the stall window
is what protects the integration's single capacity slot.

Before abandoning a run for either reason, AIST asks the provider once more whether
it has finished, and imports the result if one exists by then. A pipeline whose
outcome is `TIMEOUT` therefore means the provider had nothing to hand over, not
merely that AIST stopped waiting.

Both bounds are recorded per run when it first starts, so changing a setting affects
runs started afterwards. A run already in flight keeps the ceiling it was given.

### VPN sidecars during a long run

An execution keeps its VPN sidecar for as long as it runs. The periodic sweep that
reclaims leaked sidecars (`AIST_VPN_ORPHAN_MAX_AGE_MINUTES`, default 240) never
removes one that a live pipeline still owns, and never touches a warm-egress
sidecar, which its own idle reaper retires. Do not treat that age as a run limit:
lowering it does not shorten scans, and it is not the setting to change if a run is
taking too long.

For a schedule that does not launch, inspect its persisted last-attempt time,
error code, and safe error explanation before changing cron or readiness state.
Do not repeatedly recreate the schedule: the stored due tick is the recovery
anchor after an admission failure.

## Import an external result

In **Pipelines → Import**, select DAST, choose the enabled binding, upload the
complete provider terminal result, and review the validation preview before
confirming.

For a binding whose target requires a repository trigger, AIST resolves the
effective Git hash from the validated report entry matching the binding's
repository identity; there is no separate revision field to override, and a
report with no matching entry is rejected. For a binding whose target has no
repository requirement, the report must carry no repository identity and the
import pipeline is created without a project version. Confirmation creates an
import pipeline and repeats validation before findings are persisted.

When the report describes its run, the preview also shows the coverage and agent
token spend it reported, so an import whose run examined the wrong surface can be
abandoned instead of confirmed. The same figures appear on the pipeline
afterwards.

## Rotate or disable access

Generate a replacement onboarding bundle through the provider's rotation
operation, then rotate the integration token in AIST. Wait for the new
validation generation and capability synchronization before re-enabling
autonomous bindings.

Disable the integration when access must stop. Disable is idempotent: it blocks
new admission, supersedes pending validation and catalog synchronization work,
and disables schedules while preserving configuration and history.

Use **Delete** only for an already disabled, quiescent integration. Deletion is
blocked while a request, pipeline, or execution lease is active. A successful
teardown removes targets, bindings, presets, schedules, and DAST queue-control
records, while pipelines and findings remain available.

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
| Validation reports a rejected endpoint | Absolute HTTPS URL with no credentials, query, or fragment, on port 443 or 8443 |
| Validation reports a TLS handshake failure | Gateway certificate chain, the CA carried in the bundle, and whether the gateway serves the hostname in the gateway URL. This is a certificate or naming fault, not a network one |
| Validation reports an unreachable gateway | DAST VPN health, gateway availability, and the listening port |
| Ready, but no targets to bind | Whether the gateway accepts a target-catalog request from this integrator identity. Reaching the gateway and being allowed to list its targets are separate permissions on the DAST side, so validation can succeed while the catalog is refused |
| Binding stale | Capability revision, schema digest, target availability, and parameter snapshot |
| Launch remains pending | Stored authority, readiness, capacity, and request expiry |
| Cancellation remains pending | Provider reachability and reconciliation progress |
| A run ends as `TIMEOUT` | Whether the provider stopped delivering log output, and for how long, against `AIST_DAST_PROVIDER_STALL_TIMEOUT_SECONDS`. AIST reads the provider's status once more before abandoning a run, so this outcome means the provider had no result to hand over |
| A long scan is cut short | The two run bounds above, and whether the provider keeps emitting log output. Duration alone never ends a run |
| Import rejected | Complete terminal result, expected binding, source identity, size, report format, and whether the run metadata the report carries is well formed |
| A pipeline shows no coverage or token figures | Whether the report described its run at all. A provider that reports nothing about the run imports normally and simply has nothing to show; nothing is missing on the AIST side |
| A pipeline reports inconsistent run accounting | The provider's own breakdown against the total it reported in the same report. The import, its tests, and its findings are unaffected, so treat this as a provider reporting defect |
