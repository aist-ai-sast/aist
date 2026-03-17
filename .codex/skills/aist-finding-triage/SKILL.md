---
name: aist-finding-triage
description: Analyze new AIST pipeline findings from docker-compose data sources, classify TP/FP, and produce complete AISTAIFindingResponse payloads with developer-ready evidence and reproduction details. Use when reviewing findings by pipeline id and host source path.
---

# Inputs

- `pipeline_id` (required): AIST pipeline id (UUID-like string).
- `source_path` (required): Absolute path to source code on host.
- `severity` (optional): Defaults to `any`.

# Rules

1. Use `docker compose` only when querying project data. Do not address containers directly.
2. Analyze only new findings for the specified pipeline and severity scope.
3. Decide only `TP` or `FP` for each analyzed finding.
4. The skill must persist triage results in the project database.
5. For each analyzed finding, directly create or update `AISTAIFindingResponse`; do not stop at generating a chat response.
6. If verdict is `FP`, persist `AISTAIFindingResponse` and update the related `Finding` to the project's false-positive state using the normal project path (`false_p=True` and inactive/mitigated state as applicable).
7. Do not use `AISTAIResponse.payload -> sync_ai_finding_responses()` as the primary write path; use it only when the task explicitly requires testing the ingestion callback path.
8. Populate all real `AISTAIFindingResponse` fields that map to the schema, including `title`, `summary`/`reasoning`, `references`, `epss_score`, `impact_score`, `exploitability_score`, `uncertainty_level`, `uncertainty_spread`, and `exploit_code_maturity`. Use explicit defaults rather than omitting fields.
9. If verdict is `TP`, include a reproducible local exploit/PoC path for developers as exact executable steps, not abstract guidance.
10. Write all reasoning in English.
11. Never mention scanner/tool vendor names.
12. Ensure the response has `pipeline_id` equal to the provided input.
13. Format `reasoning` as structured Markdown (headings + lists), not plain paragraph text.

# Workflow

1. Validate inputs (`pipeline_id`, `source_path`) and confirm `source_path` exists.
2. Retrieve findings for the target pipeline and severity.
3. For each finding, map claim to concrete code/data flow evidence in `source_path`.
4. Assess exploitability, impact, and uncertainty from observed evidence.
5. Persist one `AISTAIFindingResponse` per finding directly in the database.
6. Apply the corresponding `Finding` status change, including the normal false-positive transition when verdict is `FP`.
7. Return a concise human summary of what was written to the database.

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
