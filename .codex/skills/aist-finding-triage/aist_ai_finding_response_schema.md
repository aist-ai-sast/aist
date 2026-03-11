# AISTAIFindingResponse Schema

Use this schema for each analyzed finding.

## Canonical fields

- `pipeline_id` (string, required): must equal input `pipeline_id`.
- `finding_id` (integer, required): positive integer.
- `verdict` (string, required): `true_positive` or `false_positive`.
- `title` (string, required): concise triage title.
- `reasoning` (string, required): English, human-readable, evidence-based.
- `epssScore` (number or null, required)
- `impactScore` (number or null, required)
- `exploitabilityScore` (number or null, required)
- `uncertaintyLevel` (number or null, required)
- `uncertaintySpread` (number or null, required)
- `exploitCodeMaturity` (string, required): empty string allowed.
- `references` (array of strings, required): use only valid `http/https` URLs.

## Reasoning quality requirements

`reasoning` must include:

1. Why the finding is TP or FP in this specific codebase.
2. Exact source evidence (file paths/functions/data flow), without tool/vendor mention.
3. Preconditions and constraints.
4. Risk impact statement.

For `true_positive`, also include:

1. Reproducible exploit/PoC steps for local developer reproduction.
2. Steps must be executable "as-is": explicit prerequisites, exact input values, and exact command/request payloads.
3. Expected observable result per step (response/body/log/screen effect).
4. Minimum remediation direction.

For `false_positive`, also include:

1. Why exploit path is invalid in runtime context.
2. Which guardrails invalidate exploitation.
3. Why status should be treated as false positive.

## Output format

Return a JSON array, one object per finding:

```json
[
  {
    "pipeline_id": "9a387827",
    "finding_id": 12345,
    "verdict": "true_positive",
    "title": "Unsanitized server-side template input leads to code execution",
    "reasoning": "Evidence: ... Reproduction: ... Expected result: ...",
    "epssScore": null,
    "impactScore": 8.5,
    "exploitabilityScore": 7.2,
    "uncertaintyLevel": 0.2,
    "uncertaintySpread": 0.1,
    "exploitCodeMaturity": "proof_of_concept",
    "references": [
      "https://owasp.org/www-community/attacks/Server_Side_Template_Injection"
    ]
  }
]
```

`reasoning` must use Markdown formatting with this structure:

```md
## Verdict
TP or FP with one-sentence rationale.

## Evidence
- File paths and relevant code behavior.
- Data flow summary.

## Reproduction
1. Preconditions
2. Step-by-step actions (copy-paste ready)
3. Expected result

## Impact
- Practical security impact and affected scope.

## Remediation
- Minimal actionable fix direction.
```

## Hard constraints

- Never mention scanner/tool names.
- Never leave required keys out.
- Never return non-URL items in `references`.
- Never output text outside the JSON array.
- Never provide abstract PoC text without concrete executable steps.
- Never return `reasoning` as unstructured plain text.
