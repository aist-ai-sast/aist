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
4. If verdict is `FP`, include status update intent to false positive in the response semantics.
5. If verdict is `TP`, include a reproducible local exploit/PoC path for developers as exact executable steps, not abstract guidance.
6. Write all reasoning in English.
7. Never mention scanner/tool vendor names.
8. Ensure the response has `pipeline_id` equal to the provided input.
9. Populate all response fields. Use explicit defaults rather than omitting fields.
10. Format `reasoning` as structured Markdown (headings + lists), not plain paragraph text.

# Workflow

1. Validate inputs (`pipeline_id`, `source_path`) and confirm `source_path` exists.
2. Retrieve findings for the target pipeline and severity.
3. For each finding, map claim to concrete code/data flow evidence in `source_path`.
4. Assess exploitability, impact, and uncertainty from observed evidence.
5. Produce one `AISTAIFindingResponse` object per finding.
6. Return only structured response objects without extra narrative outside schema.

# Output Contract

Follow the schema and quality checks in:

- [aist_ai_finding_response_schema.md](aist_ai_finding_response_schema.md)
