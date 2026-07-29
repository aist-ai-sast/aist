# AIST and DAST product architecture

AIST and DAST are independently deployed products with complementary roles.
AIST owns the source-to-finding workflow and durable review history. DAST owns
dynamic testing of deployed targets and the raw evidence produced by that
testing.

![AIST and DAST service boundary](../assets/aist-dast-service-boundary.svg)

## AIST owns the analysis control plane

AIST onboards projects and source versions, admits manual or scheduled runs,
invokes security-analysis runtimes, imports their reports, correlates findings
across pipelines, and presents findings for human or AI-assisted review.

DefectDojo is embedded in the AIST application and supplies the underlying test,
finding, parser, and report-import foundation. The separately maintained SAST
pipeline package creates isolated workspaces and analyzer containers for one
admitted run.

## DAST owns target-side execution

DAST is deployed separately and exposes an authenticated integration gateway.
It owns target policy, dynamic execution, raw evidence, provider-side progress,
and the terminal result produced by a run. Those responsibilities remain in
DAST even when AIST initiated the run.

AIST does not share a database, user session, credential store, or runtime with
DAST. AIST stores only the organization integration, synchronized target
capabilities, explicit project-to-target binding, pipeline control state, and
validated result needed for its finding workflow.

## How one autonomous run crosses the boundary

1. An authorized AIST user submits a one-off DAST launch or starts an enabled
   DAST launch configuration.
2. AIST validates project authority, integration readiness, target capability,
   source compatibility, and execution capacity.
3. An AIST worker starts a short-lived connector, optionally in the VPN network
   selected by the DAST integration.
4. The connector starts and observes the provider run through the DAST gateway.
5. AIST validates the returned run identity and report before importing tests
   and findings.

Cancellation and recovery cross the same connector-to-gateway boundary. AIST
owns the durable cancellation intent and pipeline outcome; DAST owns the actual
termination of target-side work.

## Manual results follow the same finding boundary

A DAST run performed outside AIST can be imported against an enabled project
binding. The import does not require gateway credentials, but it still validates
the result identity, source revision, target binding, and report format before
creating the pipeline's tests and findings.

## Continue reading

- [Pipeline execution](../product/pipeline-execution.md) — the shared launch and
  finding lifecycle
- [DAST integration](../integrations/dast.md) — configuration, readiness, and
  result boundary
- [Pipeline execution runtime](../architecture/sast-pipeline-runtime.md) —
  generic dispatch, SAST analysis, and standalone-provider execution
- [Runtime deployment](../architecture/runtime-deployment.md) — where AIST
  services and operation containers run
- [DAST operations](../runbooks/dast-operations.md) — onboarding, launch,
  cancellation, and recovery
