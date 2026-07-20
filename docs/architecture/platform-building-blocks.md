# Platform Building Blocks

This page assigns responsibility inside AIST. It is not a request trace: use a
product or data-flow page when the question is how one operation progresses.

![AIST platform building blocks](../assets/platform-building-blocks.svg)

## User-facing application

The React client provides the project, pipeline, finding, integration,
membership, and work-item screens. The Django application provides the REST
API and server-rendered AIST views. Both entry points enforce the same
authenticated user and organization-scoped authorisation model described in
[tenant isolation and access](../security/tenant-isolation-and-access.md).

## Durable application state

PostgreSQL holds the AIST domain records: organizations, projects and source
versions, pipeline runs, findings, verdicts, integrations, work-item links,
and action results. It is the durable state used by both the web application
and workers. Valkey is the Celery broker; it carries work between the
application, beat scheduler, and workers rather than acting as the source of
record for AIST data.

## Background execution

Celery Beat schedules recurring work. Celery workers execute queued operations
such as source-control discovery, validation, pipeline execution and
post-processing, AI triage, integration checks, and work-item synchronisation.
Workers update durable state and pipeline status as each operation progresses.

## Scan execution

The SAST pipeline package is invoked by the pipeline worker. It prepares the
project workspace, runs configured analyzers in Docker containers, and hands
their reports back to the platform importer. This is intentionally separated
from the interactive web process because it creates execution-specific
workspaces and containers.

## Local AI bridge

The optional local triage bridge is a separate runtime service reached through
a Unix socket shared with the worker. It is used by local AI-triage and
agent-bridge analyzer modes. Its operation-level behaviour belongs to
[AI triage](../product/ai-triage.md) and the corresponding data-flow page.

## Key models

The durable state above is implemented in `aist/models.py`. The models an
agent touches most often: `Organization` (tenant root), `AISTProject` and
`AISTProjectVersion` (a project and its source versions), `AISTPipeline` (one
scan run), `ProcessedFinding` (a deduplicated finding), and
`AISTAIFindingResponse` (an AI triage verdict). Org-scoping and permission
resolution for these models is described in
[tenant isolation and access](../security/tenant-isolation-and-access.md).
