---
name: aist-finding-triage
description: Analyze new AIST pipeline findings from docker-compose data sources, classify TP/FP, and produce complete AISTAIFindingResponse payloads with developer-ready evidence and reproduction details. Use when reviewing findings by pipeline id and host source path.
---

# Inputs

- `pipeline_id` (required): AIST pipeline id (UUID-like string).
- `source_path` (required): Absolute path to source code on host.
- `severity` (optional): Defaults to `any`.
- `output_path` (required): Absolute path to a directory the skill MUST write
  the completion marker into. The bridge polls this directory; the marker
  file appearing is the only reliable completion signal — without it the
  bridge waits for `AIST_LOCAL_TRIAGE_TIMEOUT` (default 3 hours) before
  treating the run as finished.
- `result_filename` (required): Name of the completion marker file the
  skill must create inside `output_path` as its **last** action.

# Rules

1. Use `docker compose` only when querying project data. Do not address containers directly.
2. Analyze only new findings for the specified pipeline and severity scope.
3. Decide only `TP` or `FP` for each analyzed finding.
4. The skill must persist triage results in the project database.
5. For each analyzed finding, directly create or update `AISTAIFindingResponse`; do not stop at generating a chat response.
6. If verdict is `FP`, persist `AISTAIFindingResponse` and update the related `Finding` to the project's false-positive state using the normal project path (`false_p=True` and inactive/mitigated state as applicable).
7. Do not use `AISTAIResponse.payload -> sync_ai_finding_responses()` as the primary write path; use it only when the task explicitly requires testing the ingestion callback path.
8. If verdict is `TP`, include a reproducible local exploit/PoC path for developers as exact executable steps, not abstract guidance. Write all reasoning in English.
9. Never mention scanner/tool vendor names.
10. Ensure the response has `pipeline_id` equal to the provided input.
11. Format `reasoning` as structured Markdown (headings + lists), not plain paragraph text.

# Workflow

1. Validate inputs (`pipeline_id`, `source_path`) and confirm `source_path` exists.
2. Retrieve findings for the target pipeline and severity.
3. For each finding, map claim to concrete code/data flow evidence in `source_path`.
4. Assess exploitability, impact, and uncertainty from observed evidence.
5. Persist one `AISTAIFindingResponse` per finding directly in the database.
6. Apply the corresponding `Finding` status change, including the normal false-positive transition when verdict is `FP`.
7. **Write the completion marker** as the very last action: create an empty
   file at `{output_path}/{result_filename}` (e.g. via the `Write` tool with
   empty content, or `bash`: `touch "$output_path/$result_filename"`). This
   signal is what wakes the bridge up; skipping it leaves the pipeline
   stuck for hours.
8. Return a concise human summary of what was written to the database
   (after step 7 — the marker MUST exist on disk before the summary).

# Django ORM cheat sheet

Use these queries verbatim inside `docker compose exec ... manage.py shell -c "..."`.
They are the only supported way to navigate findings ↔ pipeline ↔ AI response.
Do not invent shortcuts — `Finding` and `ProcessedFinding` have **no** `pipeline`
field; querying `filter(pipeline=...)` on them raises `FieldError`.

```python
from aist.models import AISTPipeline, AISTAIFindingResponse, AISTAIResponse
from dojo.models import Finding

# 1. Resolve pipeline by full id (the bridge passes the full id in the prompt).
pipeline = AISTPipeline.objects.get(id=pipeline_id)

# 2. List findings for a pipeline — relation is Test.aist_pipelines (M2M).
findings_qs = Finding.objects.filter(test__aist_pipelines=pipeline)
# equivalent: Finding.objects.filter(test__aist_pipelines__id=pipeline_id)

# 3. New (not yet triaged) findings for this pipeline.
new_findings_qs = findings_qs.exclude(
    aist_ai_responses__pipeline=pipeline,   # related_name on AISTAIFindingResponse.finding
)

# 4. Filter by severity (optional).
if severity and severity.lower() != "any":
    new_findings_qs = new_findings_qs.filter(severity__iexact=severity)

# 5. Create or update one AISTAIFindingResponse per finding (idempotent).
AISTAIFindingResponse.objects.update_or_create(
    pipeline=pipeline,
    finding=finding,
    defaults={
        "verdict": AISTAIFindingResponse.Verdict.TRUE_POSITIVE,  # or FALSE_POSITIVE
        "title": title,
        "summary": reasoning_markdown,
        "references": references_list,
        "epss_score": epss, "impact_score": impact,
        "exploitability_score": exploit, "uncertainty_level": unc_level,
        "uncertainty_spread": unc_spread, "exploit_code_maturity": maturity,
        "fix": fix_payload_or_None,
    },
)

# 6. Apply FP transition on the Finding itself (normal project path).
if verdict == "false_positive":
    finding.false_p = True
    finding.active = False
    finding.mitigated = timezone.now()
    finding.save()
```

Field reference (read-only — do not guess):

| Model | Has `pipeline` FK? | How to reach pipeline |
|---|---|---|
| `AISTPipeline` | self (`id`) | — |
| `AISTAIFindingResponse` | yes (`pipeline`) | direct |
| `AISTAIResponse` | yes (`pipeline`) | direct |
| `Finding` | **no** | `test__aist_pipelines` |
| `ProcessedFinding` | **no** | `test__aist_pipelines` (via `test`) |

# Output Contract

Primary outcome: persisted database changes for the analyzed findings.

Assistant response: concise human summary of the `AISTAIFindingResponse` records written and any `Finding` status changes applied.

JSON may be used as an internal intermediate format, but it is not the required user-facing output.

## Persistence notes

- This skill is intended for operational triage inside AIST.
- Success is defined by correctly updated project database state.
- The chat response is secondary to the persisted result.

Follow the schema and quality checks in:

- [aist_ai_finding_response_schema.md](aist_ai_finding_response_schema.md)
