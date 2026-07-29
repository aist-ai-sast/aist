# Pipeline execution runtime

The `sast-pipeline` package is the worker's container-execution boundary for
SAST and standalone providers such as DAST. It is not a separate long-lived
service. AIST owns durable authorization, admission, lifecycle state, and result
import; the package owns provider invocation, operation-container cleanup, and
bounded runtime outcomes.

![AIST control plane and pipeline execution runtime](../assets/sast-pipeline-runtime.svg)

## Hand one durable execution to a worker

AIST first uses its execution-driver registry to validate the selected target,
acquire capacity, and persist an **Admitted** pipeline with a recoverable publish
intent. The broker message contains only the pipeline identity. A worker accepts
one matching delivery, moves the pipeline to **Executing**, and reconstructs the
frozen execution input from PostgreSQL.

This keeps tenant authority, credentials, parameters, and provider checkpoints
out of the broker payload. A duplicate or stale delivery cannot start an
unrelated execution.

## Select the runtime path

Inside the worker, the AIST driver selects the lifecycle behavior for the
pipeline type. The execution package then uses its own registry to invoke one
runtime path:

- **SAST** creates an execution-specific workspace and output directory, runs
  the builder, then fans out to the selected analyzer containers;
- **DAST** creates a connector container that starts or resumes one external
  provider run and returns a bounded outcome plus its recovery checkpoint.

The builder prepares source and dependencies in a container selected for the
project. Analyzer containers then use the prepared workspace and the
per-pipeline analyzer selection derived from languages, time class, and launch
configuration.

When source acquisition needs a project VPN, the builder joins the
execution-specific VPN sidecar. Analyzer containers consume the prepared
workspace and do not automatically inherit that private network path.

## Hand reports back to AIST

SAST analyzers write reports to the run output directory. A standalone provider
returns its typed result through its connector. The platform importer validates
the selected format, records tests on the pipeline, and creates or updates
findings for the effective project version.

After report hand-off and container cleanup, AIST owns deduplication, enrichment,
regression detection, review, and AI triage.

## Recover without changing the boundary

The launch reconciler repairs stale dispatcher claims, durable publish intents,
and execution leases from PostgreSQL. If an accepted DAST task disappears while
the provider outcome remains recoverable, AIST republishes the same generic task
with the stored checkpoint. A lost SAST task cannot safely resume midway and
finishes with warnings instead.

Containers and temporary paths remain scoped to one pipeline. SAST cancellation
stops local work immediately. DAST cancellation persists intent first, then the
connector carries the stop request across the provider boundary until a terminal
outcome is observed.

See [pipeline execution](../product/pipeline-execution.md) for the user-visible
lifecycle and [VPN-routed operations](../data-flows/vpn-routed-operations.md)
for the conditional network path.
