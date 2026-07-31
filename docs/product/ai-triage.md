# AI Triage

AI triage adds a finding-level verdict to an imported pipeline result. It runs
only after pipeline post-processing has made findings ready for review. Two
independent decisions control it: findings are selected manually or
automatically, then the selected set is executed through the configured webhook
or local CLI backend.

![AI triage workflow](../assets/ai-triage.svg)

## Select findings

A manual request is authorised against the pipeline's project and verifies that
each selected finding belongs to that project. Automatic triage selects active
findings after post-processing, applies the saved filter for the execution
backend, and excludes findings that already have an analyzer-produced AI
verdict.

The execution backend is resolved independently of the selection trigger: a
per-launch override takes priority over the project profile. AIST supports two
backends — an **n8n webhook** and a **local Claude bridge** — and either
selection trigger can use either backend. The selection trigger, backend
override, and filter snapshot are recorded with the run, so a later
project-profile change does not rewrite a completed run.

## Receive and retain a verdict

When AIST accepts a triage request, the pipeline waits for a result. A result is
stored as one AI response per pipeline and one verdict per finding in that
pipeline. A verdict is `True Positive`, `False Positive`, or `Uncertain`; it can
include summary, references, risk scores, uncertainty values, and a suggested
fix. The findings list and detail view show the saved verdict.

In the normal callback flow a `False Positive` verdict automatically closes the
finding, marks it as false positive, and records the AI action. `True Positive`
and `Uncertain` findings remain active for human review. A reviewer can later
change any of these dispositions through the normal finding controls.

## Failure behaviour

If no findings are selected, the pipeline completes without a triage request.
If the selected local mode has no active credential, or the request cannot be
accepted, AIST finishes the pipeline with warnings. A local completion callback
that reports an error still completes without warnings when finding-level
verdicts were already retained.
