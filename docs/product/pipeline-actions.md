# Pipeline Actions

Pipeline actions are reusable operations attached to a project's launch
configuration. Each action runs when its configured pipeline status is reached.

![Pipeline action trigger and execution](../assets/pipeline-actions.svg)

## Configuration and trigger

An action belongs to a launch configuration and records its type, non-secret
settings, and `trigger_status`. The trigger can be any defined pipeline status.
When a run is created, its launch configuration is captured in pipeline data.

## Execution lifecycle

1. A pipeline reaches a new status.
2. The status signal selects actions whose trigger matches that status.
3. The pipeline records `pending`, resolves a handler, then records
   `performed` or `failed` with its error.

The action/run/status key prevents duplicate execution. A missing handler is a
visible failed action, not a silent omission.

## Current and future handlers

[Slack](../integrations/slack.md), email, and write-log are current handlers.
Slack/email resolve their organization integrations at execution time;
recipients, channels and summary options remain on the action. An AI CSV
requires the report to exist at the triggering status. New handler types
reuse this trigger model.

One-off actions can also live directly in one pipeline’s launch data. Their
generated ID is marked done after the matching status is processed.

## Implementation references

- [Action record](../../aist/models.py:1563)
- [Status-triggered execution](../../aist/celery_signals.py:236)

An action records its result on the pipeline. It does not change source version,
finding disposition, risk acceptance, or work-item status.
