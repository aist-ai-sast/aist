# SAST pipeline runtime

The SAST runtime turns one selected project version into analyzer reports that
AIST can import. It owns the isolated workspace and analyzer containers for one
run; it does not own launch authorization, finding review, or AI disposition.

![SAST runtime responsibilities](../assets/sast-pipeline-runtime.svg)

## Create one run workspace

The pipeline worker creates an execution-specific workspace and output
directory. It supplies the selected source version and resolved run parameters
to the SAST runtime. Output is partitioned by pipeline so concurrent runs do not
write to the same report location.

## Build and analyze

The builder prepares source and dependencies in a container selected for the
project. Analyzer containers then use the prepared workspace and the
per-pipeline analyzer selection derived from languages, time class, and launch
configuration.

When source acquisition needs a project VPN, the builder joins the
execution-specific VPN sidecar. Analyzer containers consume the prepared
workspace and do not automatically inherit that private network path.

## Hand reports back to AIST

Analyzers write reports to the run output directory. The platform importer
validates each supported report, records its tests on the pipeline, and creates
or updates findings for the selected project version.

The SAST runtime finishes after report hand-off and container cleanup. The AIST
control plane then owns deduplication, enrichment, regression detection, review,
and AI triage.

## Failure and cleanup

Containers and temporary paths are scoped to one pipeline. On completion,
cancellation, or handled failure, the runtime stops the containers it started
and returns a bounded outcome to the worker. Durable cancellation and recovery
remain platform responsibilities because they must survive a runtime or worker
restart.

See [pipeline execution](../product/pipeline-execution.md) for the user-visible
lifecycle and [VPN-routed operations](../data-flows/vpn-routed-operations.md)
for the conditional network path.
