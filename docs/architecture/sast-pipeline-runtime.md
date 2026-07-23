# SAST Pipeline Runtime

The SAST runtime is the execution component behind an AIST pipeline run. Its
job is to turn one selected project version into analyzer reports that the
platform can import. It is not the API that starts the run and it is not the
finding review workflow.

![SAST runtime responsibilities](../assets/sast-pipeline-runtime.svg)

## Per-run workspace

The pipeline worker creates a run-specific build path and output directory.
The output directory is additionally partitioned by pipeline identifier, so
concurrent runs do not write to the same report location. The worker supplies
the selected project version, resolved run parameters, and launch environment
to the pipeline package.

## Builder and analyzer containers

The pipeline package prepares the source workspace and generates a
per-pipeline analyzer configuration from the enabled languages, time class, and
selected analyzers. It runs the builder and configured analyzers as Docker
containers. Container names include the pipeline identifier, which lets the
runtime terminate containers belonging to a failed or completed execution.

For a pipeline configured with a project VPN integration, the worker starts an
execution-specific VPN sidecar and runs the builder container in that sidecar's
network namespace. Analyzer containers mount the builder's volumes but do not
inherit its network namespace. In particular, the `sast-dast` connector
currently calls its remote gateway directly. The sidecar lifetime and
interactive-source route are documented separately in the VPN data-flow page.

## Report hand-off

Analyzers write reports into the run's output location. The platform then
imports those reports, records the affected tests on the pipeline, and either
finishes an empty result or starts the deduplication and enrichment stage. The
runtime itself does not decide the final finding disposition or AI verdict.

See [pipeline execution](../product/pipeline-execution.md) for the user-visible
pipeline states and [finding review](../product/finding-review.md) for the
records created from imported reports.
