# Platform building blocks

AIST separates interactive product requests, durable coordination, background
work, and execution runtimes. This page explains what each block owns and how
the blocks depend on one another. For the deployment topology, see
[runtime deployment](runtime-deployment.md).

![AIST platform building blocks](../assets/platform-building-blocks.svg)

## Experience and control plane

The React client is the main product interface. It presents organizations,
projects, launch configuration, pipelines, findings, integrations, and
remediation work without owning authorization or workflow state.

The Django application is the control plane behind both the client and the
public API. It authenticates the principal, resolves organization and project
scope, validates input, reads or changes durable state, and enqueues work that
must run outside the request. The same server-side authorization model applies
regardless of which client initiated the request.

## Durable coordination

PostgreSQL is the product source of truth. It stores tenant configuration,
projects and source versions, launch requests, pipeline history, findings,
review decisions, integrations, and external work-item state. Web processes and
workers coordinate through these records rather than through process memory.

Valkey is the Celery transport. It carries task delivery between producers and
workers, but it does not replace the durable launch or pipeline records in
PostgreSQL. A broker retry or duplicate delivery therefore does not define the
product outcome on its own.

## Background work

Celery Beat produces recurring work such as schedule evaluation, synchronization,
and cleanup. Celery workers consume queued operations and update the same
PostgreSQL records that the control plane exposes to users.

Workers own operations that may be slow, retried, or dependent on external
systems: repository discovery, source acquisition, pipeline execution, report
processing, AI triage, integration validation, and work-item synchronization.

## Execution boundaries

A worker delegates the execution-specific portion of a run to one of three
boundaries:

- the SAST runtime prepares an isolated workspace and starts builder and
  analyzer containers;
- a standalone connector communicates with an external execution provider such
  as DAST without joining the SAST analyzer fan-out;
- the local AI bridge starts an isolated CLI operation and is reached through a
  Unix socket shared with the worker.

These runtimes produce reports or verdicts; they do not own tenant access,
pipeline admission, finding disposition, or review history. Results return to
the control plane through the platform import or callback boundary and become
durable product state.

## Reading the relationships

An interactive request normally travels through the client and Django control
plane. Django reads or changes PostgreSQL and may enqueue work in Valkey.
Recurring work begins with Celery Beat. Workers consume the task, re-resolve the
durable records they need, invoke the selected execution boundary, and persist
the outcome back to PostgreSQL.

The sequence inside one pipeline is described in
[pipeline execution](../product/pipeline-execution.md). The SAST-specific
runtime is described in [SAST pipeline runtime](sast-pipeline-runtime.md).
