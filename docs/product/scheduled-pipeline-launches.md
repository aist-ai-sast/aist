# Scheduled Pipeline Launches

A launch schedule turns a project's launch configuration into durable queued
pipeline requests at configured cron times.

![Scheduled launch queue and dispatcher](../assets/scheduled-pipeline-launches.svg)

## Schedule definition

A schedule belongs to one launch configuration and stores a five-field cron
expression, enabled state, and `max_concurrent_per_worker`. The API validates
and previews ticks; a user can enqueue one run without changing the schedule.

## Create queue intent

Celery Beat evaluates enabled schedules. A due tick not covered by `last_run_at`
creates a durable queue item with the project, schedule, and launch
configuration, then advances `last_run_at`. Invalid cron is logged and skipped.
Scheduling never starts a pipeline directly.

## Dispatch when capacity permits

The dispatcher reads undispatched entries FIFO and compares active pipeline
tasks on each worker with `max_concurrent_per_worker`. At capacity it leaves
items queued. If worker inspection is unavailable, it logs that condition and
continues rather than blocking the queue indefinitely.

Before dispatch, it resolves the configured source version and locks it. An
unfinished or just-dispatched run for that version leaves the item undispatched.
On success it creates the pipeline, queues its worker task, stores the task ID,
and links/marks the queue item dispatched. Authorized users can view, delete,
or clear old dispatched entries.

## Implementation references

- [Schedule-to-queue task](../../aist/tasks/launch_schedule.py:12)
- [Guarded FIFO dispatcher](../../aist/tasks/pipeline_dispatcher.py:17)
