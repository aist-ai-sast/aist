# DAST integration

The DAST integration connects one AIST organization to an external DAST
gateway. It supports autonomous scans and manual result import without making
DAST part of the SAST analyzer fan-out.

## Responsibility boundary

| AIST owns | DAST owns |
|---|---|
| Organization access, project binding, launch admission, and pipeline history | Target policy, target-side execution, and provider progress |
| Encrypted integration credential and synchronized capability snapshot | Gateway authentication and the provider contract |
| Target-bound report validation, import, deduplication, finding review, and retention of what a report says about its run | Raw evidence, terminal result, report production, and the coverage and cost it reports |

The products do not share users, sessions, credentials, or a database. The DAST
gateway is an external HTTPS boundary, optionally reached through the VPN
selected on the DAST integration.

## Configure the integration

1. Import the provider-generated onboarding bundle into the organization.
2. Select an organization VPN when the gateway is on a private network.
3. Validate gateway connectivity and credentials.
4. Synchronize the provider's targets and capabilities.
5. Bind an eligible target to one AIST project. A target that declares a
   repository-trigger requirement also needs a source identity for the
   binding; a target with no such requirement (a fixed-surface or
   declared-stand scenario) is bound without one, and the binding rejects a
   source identity if one is supplied.
6. For a target that requires a repository trigger, choose an explicit Git
   branch or commit and either create a reusable DAST launch configuration or
   submit a one-off launch. For a target with no repository requirement, the
   launch configuration or one-off launch carries no trigger version at all —
   there is nothing to choose.

Only one DAST integration is active for an organization at a time; disabled
records remain as history. The service token is write-only after import.

Because that token cannot be read back, changing the stored connection — gateway
URL, CA bundle, integrator identity, or fingerprint — requires a bundle that
carries the matching token. The name and the VPN route can be changed on their
own, without a bundle.

Replacing the connection or moving the integration to a different route
revalidates it, so the integration is not ready again until the new probe
succeeds. A rename cannot change what a probe would reach, so a ready
integration stays ready through it.

## Readiness

A launch is ready only when the integration is healthy, synchronized
capabilities are current, the project binding is enabled, the parameter set is
valid, and a private gateway has its explicit DAST VPN route.

Readiness answers whether this binding can launch at all; it carries no separate
permission to launch without an operator. A binding an operator can start is a
binding a schedule can start.

The same readiness result is shown during configuration and checked again when
execution is admitted. A stale capability revision or changed provider schema
must be reviewed before the binding becomes ready again.

Launch parameters are deliberately secret-free. Provider or application scan
credentials remain with the DAST target; the synchronized parameter schema may
not introduce credential-shaped fields or secret defaults. The service token
and optional CA are loaded only at execution time from the integration.

## Autonomous execution

An authorized start enters the normal durable launch lifecycle; see
[how one autonomous run crosses the boundary](../product-architecture/README.md#how-one-autonomous-run-crosses-the-boundary)
for the full connector-to-gateway walkthrough with its diagram. After a worker
accepts the generic pipeline task, the shared execution runtime starts the DAST
connector container, which loads this integration's gateway URL and service
token and starts, observes, and can cancel the provider run. AIST retains
cancellation intent, pipeline state, and the final import decision throughout.

A DAST scan is expected to be long. Nothing caps it at a working day, and the
platform does not treat duration as a fault: a run is bounded by whether the
provider is still delivering, not by how long it has been going. Two limits sit
above that, both operator-configured — a ceiling on total run length that a
healthy scan never reaches, and a window in which the provider must show some
sign of life. Only silence ends a run early. See
[DAST operations](../runbooks/dast-operations.md#cancel-and-recover) for the
settings and how to read the resulting pipeline outcome.

Before the platform gives up on a run for either reason, it asks the provider one
last time whether that run has finished. A scan that completed while AIST was no
longer waiting is imported normally rather than recorded as lost.

The connector validates the run and correlation identities returned by the
gateway. When the provider reaches a terminal state, the execution runtime
atomically writes the report itself as `dast_result.json` in the pipeline's
standard durable analyzer output directory. AIST then persists the execution checkpoint
before it reads, validates, or imports that file. Connector credentials,
recovery state, outcome, and telemetry remain in the ephemeral execution
workspace.

AIST imports that file through the same report validator used for an operator
upload. The validator accepts only the DAST scan type, an array of findings, a
non-empty provider run identity, the target named by the selected binding, and
source revisions whose repository identities the binding allows. It also
enforces the report-size limit. The registered importer validates individual
findings. A target mismatch, an unadvertised repository identity, or an
oversized report fails before findings are persisted.
[Reported run metadata](#reported-run-metadata) covers how descriptive values
are handled.

## Manual result import

A DAST result produced outside AIST can be imported without contacting the
gateway. A user with project-operate permission selects an enabled binding,
uploads the report the provider exported, reviews its validation preview, and
confirms the import.

The uploaded file is the exported report itself — the one artifact the provider
writes for a person to carry. Nothing has to be wrapped around it: the run, the
stand, and the source revisions it was produced from are all stated inside the
report, and a wrapper could only repeat them or vouch for itself. Every check the
autonomous path makes about a report is made here too, by the same validation, and
what the binding knows — its target and the repositories it accepts — is enforced
against the upload.

For a binding whose target requires a repository trigger, AIST resolves the
effective Git hash from the validated report entry whose repository identity
matches the selected binding; it does not accept a separate client-supplied
revision, and a report with no matching entry is rejected. For a binding whose
target has no repository requirement, no repository identity is expected in the
report, and the results attach to the version standing for the target itself —
see [pipeline execution](../product/pipeline-execution.md).
Confirmation creates an import pipeline and applies the same result validation
and finding lifecycle used by an autonomous result.

## Reported run metadata

Besides its findings, a report may describe the run that produced them. AIST
keeps whatever a validated report carries against the pipeline it was imported
into — for an autonomous run and an operator upload alike — and presents it
there: headline counters on the pipeline entry, the full inventory when that
entry is expanded, and the same counters in the upload preview before the
operator confirms the import.

A report can describe two things:

- **Coverage** — what the run saw, counted in the unit the provider works in,
  such as an endpoint: how much was discovered, how much of that was reachable,
  how much was analysed, and how much the run had planned to analyse. A report
  may also name every analysed item and mark which of them the run took on beyond
  its plan. A run can exceed its own plan, so a planned figure below the analysed
  figure is a normal result, not a contradiction.
- **Agent token usage** — the model-token accounting for the run: a total, plus
  breakdowns by run phase and by agent type, each of which may report how many
  agents it used and how many model calls it made. The headline total covers
  submitted, generated, and cached tokens; thinking tokens are reported alongside
  it rather than added to it, because the provider already counts them inside its
  generated total.

Every descriptive field at every level is optional, including the two
descriptions themselves. Absence, an empty string, and a value AIST cannot
interpret all become an absent value in the corresponding metadata column; none
of them prevents finding import. A derived number — a total, a duration, or a
beyond-plan count — is withheld unless everything it needs was understood.
Empty strings, lists, and objects therefore have the same stored meaning as an
omitted value: `NULL`.

These figures are the provider's account of its own run. AIST stores them and
shows where they disagree with themselves; it does not re-measure them. The
single report-size limit bounds the complete input, including these descriptions.

### How a report's description is judged

| What the report carries | What AIST does |
|---|---|
| A descriptive value AIST cannot interpret: the wrong type, an empty string, list, or object, or a negative count | Stores `NULL` for the affected metadata value and imports the findings normally |
| A descriptive field this version of AIST does not recognize — in the run metadata, in either description, or on an individual finding | Reads past it and imports the report normally. The stored report keeps the field exactly as it arrived |
| A breakdown that is well formed but does not add up to its own reported total | Accepts it and marks the run's accounting as inconsistent, so the pipeline shows the disagreement. The findings are unaffected |

The middle row is deliberate. A provider that has learned a new field is a newer
provider, not a broken one, and a report full of real findings should not be lost
over a description AIST has nowhere to put. It applies only to descriptive
material that no decision is made on; a finding that omits something the platform
requires is still refused.

The fields AIST acts on remain strict: the scan type selects the importer, the
run identity supports idempotent delivery, the target binds the file to the
selected project binding, and source-commit keys stay inside that binding's
repository set. Extra report-envelope fields and descriptive metadata do not
make authorization or attachment decisions and therefore do not reject the
report.

## Network and credentials

Private DAST traffic uses only the VPN attached directly to the DAST
integration; project and SCM VPN configuration does not substitute for it.

Every gateway URL must be an absolute HTTPS URL with no credentials, query, or
fragment, on port 443 or 8443. Configuration validation and the connector
runtime enforce the same allowlist, so an integration on either port validates
and launches consistently. How far the destination address can be checked
depends on which side resolves it for the connection that follows:

- An address literal needs no lookup, so the address checked is the address used.
  It must be public, or — when the integration has an active VPN route — a
  standard private address. Loopback, link-local, and other special-purpose
  addresses are never accepted.
- A name on a direct connection is resolved by the platform and must be public,
  because that lookup is the one the connection uses.
- A name on a VPN-routed connection is resolved inside the tunnel, by the proxy
  that opens the connection. The platform cannot confirm that address from
  outside the tunnel: a VPN-internal name usually has no answer there, and an
  answer that does exist describes a different lookup than the connection makes.

For a routed name, then, the VPN route attached to the integration is what bounds
where the connection can land; give the route no more reach than the gateway
needs. The platform still refuses a routed name that it can already see resolves
to a loopback, link-local, or other special-purpose address. That is an early
rejection of an obvious misconfiguration, not a guarantee about the address the
connection finally uses.

A routed name that cannot be looked up at all is a different case from one that
resolves somewhere forbidden. Inside a tunnel that is still settling there may be
no answer yet, and no answer is not a policy violation — the attempt proceeds and
the connection itself is retried. What bounds the destination in that case is
still the route and the port allowlist, neither of which depends on a lookup.

A rejected destination fails before any connection is attempted. A failure of the
TLS handshake itself — an untrusted certificate, or a name the gateway will not
serve — is reported separately from an unreachable gateway, so the two are not
diagnosed as the same fault.

The service token and optional custom CA are loaded from the organization
integration when the connector starts. They are not launch parameters or
general broker payloads.

## Disable and teardown

Disabling an integration is the normal reversible stop. It prevents new DAST
admission, invalidates in-flight validation and synchronization work, and
disables its schedules while preserving targets, bindings, presets, pipelines,
findings, and onboarding history.

Deletion is a separate quiescent teardown. It is available only after disable
and only when no launch or execution remains active. Teardown removes DAST
targets, bindings, presets, schedules, and queue-control records, but retains
pipeline and finding history. This separation prevents a UI delete action from
silently removing the durable state required to recover active work.

Use the [DAST operations runbook](../runbooks/dast-operations.md) for API
examples, cancellation, recovery, contract promotion, and token rotation. Use
the [staging compatibility canary](../runbooks/dast-staging-canary.md) before a
provider contract or deployment change is promoted.
