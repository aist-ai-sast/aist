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
| Relaying one authorized operator decision into a running scan's work queue | The work queue itself: its vocabulary, which actions exist on each item, who may send them, and what each one does |

The products do not share users, sessions, credentials, or a database. The DAST
gateway is an external HTTPS boundary, optionally reached through the VPN
selected on the DAST integration.

## Configure the integration

1. Import the provider-generated onboarding bundle into the organization.
2. Select an organization VPN when the gateway is on a private network.
3. Validate gateway connectivity and credentials.
4. Wait for the provider's targets and capabilities to synchronize. A
   successful validation starts this automatically; see
   [catalog synchronization](#catalog-synchronization).
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

## Catalog synchronization

Synchronization copies the provider's target catalog and capability snapshot
into AIST. It always runs in a background worker, never in the request that
asks for it. Three things start one:

- a successful validation, which reserves the synchronization in the same step
  that marks the integration ready, so a ready integration never exists without
  a synchronization completed or on its way;
- an operator choosing **Synchronize** on the integration;
- a periodic refresh that keeps a ready catalog inside the freshness window that
  launch [readiness](#readiness) requires.

The integration reports the state of its latest synchronization:

| State | Meaning |
|---|---|
| Idle | No synchronization has been requested since the integration was created or disabled |
| Pending | Requested and waiting for a worker |
| Running | A worker is fetching the catalog from the gateway |
| Succeeded | The catalog is current as of the recorded synchronization time |
| Failed | The latest attempt failed; its error code says why |
| Stalled | Pending or running for more than 30 minutes, so the attempt is presumed lost and a new one should be started |

Only the latest request counts. A newer request supersedes an older one, and a
superseded attempt that finishes late cannot change the result or the state of
the request that replaced it. The periodic refresh leaves a running attempt
alone for the same 30 minutes before reserving a replacement.

When the provider reports that the catalog is unchanged, synchronization still
succeeds and records that every available target was seen again, so a target's
last-seen time reflects the latest successful check rather than the latest
catalog change.

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
upload. The validator accepts only the DAST scan type, a flat findings array or per-repository
result sections, a
non-empty provider run identity, the target named by the selected binding, and
source revisions whose repository identities the binding allows. It also
enforces the report-size limit, and refuses a report or finding value the
platform cannot store — one longer than the column that stores it, or one
carrying a NUL. The refusal names every offending finding and field, so a
provider learns in one upload what it has to shorten. The registered importer
validates individual findings. A target mismatch, an unadvertised repository
identity, an oversized report, or an unstorable value fails before findings are
persisted.
[Reported run metadata](#reported-run-metadata) covers how descriptive values
are handled.

## The run's work queue

While a DAST run exists, the work it still owes can be read — and, with the
right gateway permission, steered — from the administrative AIST pipeline page
(superusers only). It lists the job currently holding a claim, the jobs waiting
behind it, the results the run has yet to record, any operator hold, and the
leads the run declined to admit.

A queue is addressed by the provider's own run identity, and AIST records that
identity as soon as the connector reports the provider has accepted the run —
not when the run ends. A scan can therefore be watched and steered while it is
still going. A run that has started but has not been accepted yet has nothing to
address, and the block says what it is waiting for rather than vanishing. A
finished pipeline that never recorded a run identity gets no block at all.

AIST holds no DAST vocabulary of its own here. The gateway publishes a
descriptor for the run, and that descriptor carries the wording of every
status, wait reason, phase, and agent contract, the generic icon that stands
for each one, the catalog of actions and what each one does, the fields each
action takes, and how often the gateway wants to be asked. Each queue row
carries what may be done to that row. AIST renders what arrives: an identifier
the descriptor does not define is shown as it arrived, and a form control this
AIST version has no renderer for is shown read-only. A status, an action, or an
editable field added on the DAST side therefore reaches the operator without an
AIST release.

The provider's contract for this surface is maintained with the DAST product
rather than here, as `contracts/dast-integration.openapi.json` with a worked
example per kind of target — a finished run among them — under
`contracts/examples/queue/`. Addresses inside the payload are relative to the
gateway's own `/integrations/v2/` prefix.

### The page never reaches the gateway

The route to a gateway runs through the integration's VPN sidecar and the
container control that only a background worker has; the process that serves the
page has neither. A read therefore renders the last queue a worker stored for
this pipeline, and a command is handed to a worker to send. That stored queue
belongs to the pipeline, so it stays readable as the record of a finished run
even once the gateway no longer serves it.

### What the gateway has to grant

Queue access is granted per integrator on the DAST side, and nothing is granted
by default:

| Gateway capability | What it allows |
|---|---|
| `queue:read` | Reading the run's descriptor and its queue |
| `queue:control` | Pausing, resuming, cancelling, delaying, editing, or admitting work |
| `run:annotate` | Recording an operator note against a row |

An integrator that holds none of them has no queue to show, and the block says
so in one quiet line rather than reporting a fault. An integrator that holds
only `queue:read` is served rows with no controls; the block states that it is
read-only and names what the integration does hold. Granting these capabilities
is a deployment step on the DAST side.

### Two states, read separately

A queue page carries two states that answer different questions. The queue's own
state — active, pausing, or paused — is about operator holds. The run's
lifecycle is separate, and a finished run whose queue nobody paused still
reports that queue as active. The block therefore leads with the run's own
lifecycle, and it is the page, not AIST, that says whether anything in that
queue may still be changed.

A finished run is read once and never polled again; its queue is a record of
what the run did, not something that will move. Whether such a page still offers
any control is the gateway's answer, and in practice the one it keeps offering
is releasing an operator hold.

### Cadence, freshness, and cost

How often to ask is the gateway's decision. The descriptor carries the interval
— one minute today — and the block re-reads on it. When the stored queue is
older than that interval, a worker is asked for a fresh one and the block says a
read is on its way while it keeps showing what it already has. **Refresh** asks
for that read at once instead of waiting for the interval; it is still a worker
that goes to the gateway.

Upstream this costs one read per interval however many pipeline pages are open,
because they all render the same stored queue and a stale one asks for at most
one refresh in a short window. The worker asks conditionally, so a queue that
has not moved costs no page at all. A read that fails leaves the previous queue
in place with its own age on it rather than emptying the block over one bad
call. A command is followed by a fresh read, so what the operator sees next
reports what the command actually did.

Collecting a command's answer costs the gateway nothing: it reads what the
worker left behind. It is therefore bounded separately from the reads and
commands that do leave the deployment, so a page waiting on one answer cannot
spend the budget the next command needs.

### What an operator may send

An operator names an action and the row it applies to, and fills in the fields
that action declared. Nothing else about the outbound request comes from the
browser. The worker that sends it re-reads the row from the gateway, takes the
method and the address from the affordance the gateway published for it, drops
every field that action did not declare writable, and refuses a form field that
asks for a credential — the queue has no business collecting one. An action a
row does not advertise has no address, so an action this integrator was never
granted stays unreachable.

Each command carries the queue revision the operator decided against and an
identifier for the decision itself. A run that has moved on refuses the command
instead of applying it to a queue the operator never saw, and an identical retry
returns the first receipt instead of applying twice.

Because a worker is what reaches the gateway, the command is accepted before it
is sent: AIST confirms it holds the decision, and the gateway's answer arrives
afterwards under that same identifier, left where the process serving the page
can collect it — see
[runtime deployment](../architecture/runtime-deployment.md#state-and-queued-work).
A refusal is shown as the gateway wrote it, including which field was at fault
and what that field allows. An answer is collectable only by the operator who
sent it, and only for a while — and not at all while that shared store is
unreachable, although the command itself was still sent. After that the queue
itself is the record.

The endpoints behind the block require project read access to read a queue and
project-operate permission to command one — the same authority as stopping a
pipeline. See
[access control and roles](../security/access-control-and-roles.md).

### When part of a payload cannot be read

The payload is bounded and shape-checked before anything renders it or acts on
it, and a failure is made to cost as little as it can. One row this AIST version
cannot read costs that row: it still counts in the run's totals, and the number of rows
left out is shown, so the block never quietly lists fewer items than the run
owes. One action AIST will not send costs that control rather than the row it
sits on. Only a page envelope with no usable revision costs the whole page,
because the revision is what a command is decided against.

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

## Reports covering several repositories

A deployed target may contain several repositories. Its report can carry a
`results` array in place of the flat `findings` array. Each section names its
`repository`, may describe its `version`, and carries its own `findings` array.
Repository keys must be non-blank and unique across sections. A report cannot
carry both forms at once. Source revisions remain in
`dast_run_metadata.source_commits`; a section's descriptive version does not
choose the Git revision to which findings attach.

For example, the content of a report for two repositories has this shape,
alongside its usual run metadata:

```json
{
  "name": "DAST",
  "type": "DAST Autonomous Scan",
  "results": [
    {"repository": "nx", "version": "nx@155f8d82", "findings": []},
    {"repository": "cloud_portal", "version": "cloud_portal@5db4fd5d", "findings": []}
  ]
}
```

AIST resolves each section against enabled project bindings of the same target
in the same organization. Exactly one matching source-repository key, together
with a reported source commit, routes the section to that binding's project and
commit version. A missing or disabled binding, an ambiguous match, or a missing
source commit keeps the section in the launching project on that project's
resolved version. Configuration gaps therefore do not discard findings. The
existing flat format still imports entirely into the launching project.

The import preview shows each repository, finding count, destination project,
source commit, and any fallback reason. Preview requires read access to every
destination; confirmation requires project-operate permission for every
destination. An inaccessible destination rejects the request without revealing
its identity. The worker checks authority for additional destination projects
again before writing results, including for autonomous runs.

All sections pass through the same importer in one transaction. An error in any
section rolls back the whole import. One pipeline remains in the launching
project, owns the run's duration and cost, and links all imported tests. Each
project's findings attach to its own version. The destination binding is saved
with each test, so its findings can be compared against that project's history
regardless of which project launched the run. See
[canonical deduplication](../aist-canonical-dedupe.md) for namespace and matching
rules.

Redelivery to a finalized pipeline returns its saved destinations without
importing again or recalculating routes. Uploading the same file again through
the same launch binding reuses the active or successfully imported pipeline.
An attempt that ended before importing results can be retried. A changed report
cannot replace a report already finalized on a pipeline.

## Reported run metadata

Besides its findings, a report may describe the run that produced them. AIST
stores accepted metadata against the pipeline for autonomous runs and operator
uploads alike. The import preview and pipeline list show headline counters; the
pipeline detail API carries the full accepted inventory and diagnostics.

| Report data | Stored and exposed by AIST |
|---|---|
| Run identity and context: target, run, stand, descriptors, and timestamps | Stored with the pipeline; identity is also used for binding and delivery checks, while context is returned by the detail API |
| Coverage counts and analysed/beyond-plan names | Headline counts appear in the preview and pipeline list; names and derived duration/overshoot appear in the detail API |
| Agent token totals and per-phase/per-agent breakdowns | Headline total appears in the preview and list; breakdowns appear in the detail API. Thinking tokens remain separate because the provider already includes them in output |
| Provider `economy`: tokens per check, total tokens, checks, commands, waits, artifact reads, and contract reads | Recognized non-negative counters are stored as provider measurements without AIST recomputation. The detail API returns the stored `tokens` counter under the unambiguous name `total_tokens` |
| Delivery quality, audit state, and finding completeness | Stored with the pipeline and returned by the summary and detail APIs |
| Operator actions and excluded-finding diagnostics | Stored with the pipeline and returned by the detail API. An exclusion normally carries equal `finding_ref` and `check_id`; `CHECK_PROVENANCE_INVALID` may be identity-free when the producer could not establish that identity safely |

Coverage may use a provider-defined unit such as an endpoint. A run can analyse
more than it planned, so a planned figure below the analysed figure is a normal
result, not a contradiction.

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
| An unrecognized report-envelope or run-metadata field | Reads past it and imports the report normally; recognized metadata fields are stored |
| An unrecognized field on an individual finding | Rejects the import instead of silently discarding finding data |
| A breakdown that is well formed but does not add up to its own reported total | Accepts it and marks the run's accounting as inconsistent, so the pipeline shows the disagreement. The findings are unaffected |

The tolerant metadata row is deliberate. A provider that has learned a new field is a newer
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
