# Pipeline Execution

A pipeline is the record of one SAST run for one project version. It is created
before asynchronous work starts and remains the place where imported tests,
findings, progress, and later AI responses are connected.

![Pipeline execution from launch to review](../assets/pipeline-execution.svg)

## Start a run

A user with edit access to the project can start a run for a project version
directly or start one from a saved launch configuration. A launch configuration
stores reusable run parameters; a request can override those parameters when it
starts the pipeline.

A schedule and its “run once” action do something different: they create a
**Pipeline Launch Queue** item first. This PostgreSQL record waits for the
dispatcher. The dispatcher applies the schedule's per-worker concurrency limit,
prevents an overlapping run for the selected project version, creates the
pipeline, and queues its worker task.

The launch queue is not Celery. It answers *which scheduled runs are waiting to
start?* A pipeline answers *what happened during a run that has started?*

## Execute and import

The worker locks the pipeline before it starts it, so a duplicate task delivery
does not create a second execution. It prepares a workspace and output location
for that pipeline, runs the configured SAST pipeline, then imports analyzer
reports as tests and findings.

The imported tests are attached to the pipeline. Imported findings are attached
to its project version. A run with no findings completes after import.

## Prepare findings for review

When import creates findings, AIST waits for deduplication and duplicate cleanup
for every imported test. It then enriches the remaining findings with line hashes
and source links, runs evolution deduplication and regression detection, and
moves the pipeline to the point where it can be reviewed or sent to its
configured AI-triage path.

An execution or processing failure does not remove the run. Terminal completion
records either **Finished** or **Finished with warnings**; both outcomes can be
reached from an active stage.
