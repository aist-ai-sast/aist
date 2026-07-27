# Scheduled pipeline launches

A launch schedule runs one saved launch configuration at recurring cron times.
Each due tick first creates a durable launch request. A pipeline is created only
after the request is still authorized, ready, and within its execution
capacity.

![Scheduled pipeline launch lifecycle](../assets/scheduled-pipeline-launches.svg)

## Configure the schedule

Each launch configuration can own one schedule with:

- a standard five-field cron expression;
- an enabled or disabled state;
- a maximum of 1–8 concurrent runs for that schedule.

The schedule preview shows upcoming times in the server's configured timezone.
Changing a schedule affects future ticks; it does not rewrite pipelines or
launch requests that already exist.

Creating or changing a schedule requires project-operate permission. Readers
who can access the project can view the schedule and its resulting pipeline
history.

## From a due tick to a pipeline

Celery Beat evaluates enabled schedules. For each unprocessed due tick, AIST
records one launch request containing the selected launch configuration and the
non-secret execution inputs needed to reproduce that request. Recording the
request does not start a scan.

The dispatcher then:

1. confirms that the schedule and project authority are still valid;
2. resolves the execution target and checks its current readiness;
3. waits for a capacity slot when the selected resource is busy;
4. creates the pipeline and publishes its worker task when admission succeeds.

Requests ready at the same time are considered by priority and then age. When
an equivalent request is already waiting, AIST preserves one pending request
and marks the replaced request as superseded rather than starting duplicate
work.

## Waiting and terminal states

| Launch-request state | Meaning for the reader |
|---|---|
| Pending | Waiting for dispatch time, readiness, or capacity |
| Superseded | Replaced by an equivalent pending request; no pipeline was started |
| Dispatched | A pipeline was created and its worker task was admitted |
| Expired | Capacity was unavailable until the request deadline |
| Failed | Authority, readiness, or execution planning could not be validated |
| Cancelled | The request was cancelled before execution was admitted |

A capacity wait does not create an empty pipeline. The request is retried later
and remains visible. Once dispatch creates a pipeline, execution state and
cancellation belong to that pipeline.

## Run once

**Run once** queues the selected schedule immediately without changing its cron
expression or recorded last tick. It follows the same durable request,
authorization, capacity, and execution path as a scheduled tick.

See [pipeline execution](pipeline-execution.md) for the lifecycle after a
pipeline is created and [pipeline actions](pipeline-actions.md) for
notifications triggered by pipeline status.
