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
- `fix` (object or null, optional): remediation guidance. Omit or set to `null` for `false_positive` verdicts. See [Fix object](#fix-object) below.

## Fix object

Present only for `true_positive` (and when relevant for `uncertain`). Must be `null` or omitted for `false_positive`.

### Fields

- `fixType` (string, required): one of `code_change` | `config_change` | `architectural`.
  - `code_change` — a concrete source-code edit resolves the issue.
  - `config_change` — a configuration file, Dockerfile, or infrastructure change resolves it.
  - `architectural` — no single-file patch is sufficient; a design review or structural change is required.
- `fixSummary` (string, required): one sentence describing what the fix does and why. Max 1024 characters.
- `diffAvailable` (boolean, required): `true` if `diff` is populated, `false` otherwise.
- `diff` (string or null): unified diff in standard `--- a/` / `+++ b/` format. Required when `fixType` is `code_change` or `config_change`. Must be `null` for `architectural`.
- `codeAfter` (string or null): complete final state of the affected file section after the fix is applied. Helps the developer see the end result without parsing the diff. `null` when not applicable.
- `stepByStep` (array of strings, required): ordered remediation steps prefixed `Step N:`. Must be actionable and copy-paste ready. Max 20 items.
- `testingHint` (string or null): how to verify the fix was applied correctly (command to run, expected output, etc.).
- `secretsManagement` (string or null): specific recommendation for handling secrets or credentials in the affected stack. `null` when not applicable.
- `suppressionAnnotation` (string or null): exact inline comment to paste above the flagged line to suppress the finding if the risk is accepted. `null` when suppression is not appropriate.

### Fix type guidance

| Scenario | `fixType` | `diff` |
|---|---|---|
| Replace hardcoded value, fix input handling, add validation | `code_change` | required |
| Add `USER` to Dockerfile, change env var, update config file | `config_change` | required |
| Requires threat model, library replacement, auth redesign | `architectural` | `null` |

### Quality requirements

- `diff` must apply cleanly: include correct file paths, line context, `@@` chunk headers.
- `stepByStep` steps must be executable as-is — include exact commands, file paths, and values.
- Do not populate `fix` for `false_positive` verdicts.
- `fixSummary` must describe the security benefit, not just the mechanical change.

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

Primary outcome: persisted database changes, with one complete `AISTAIFindingResponse` written per finding.

Assistant response: concise human summary of what was written to the database and which `Finding` status changes were applied.

JSON may be used as an internal intermediate format when preparing writes, for example in this shape:

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
    ],
    "fix": {
      "fixType": "code_change",
      "fixSummary": "Replace direct template rendering with a sandboxed engine to prevent arbitrary code execution via user-controlled input.",
      "diffAvailable": true,
      "diff": "--- a/src/render/template_engine.py\n+++ b/src/render/template_engine.py\n@@ -14,7 +14,8 @@\n-    return jinja2.Environment().from_string(user_template).render(ctx)\n+    env = jinja2.SandboxedEnvironment()\n+    return env.from_string(user_template).render(ctx)",
      "codeAfter": "from jinja2.sandbox import SandboxedEnvironment\n\ndef render_user_template(user_template: str, ctx: dict) -> str:\n    env = SandboxedEnvironment()\n    return env.from_string(user_template).render(ctx)",
      "stepByStep": [
        "Step 1: Open src/render/template_engine.py and locate the jinja2.Environment() instantiation.",
        "Step 2: Replace jinja2.Environment() with jinja2.SandboxedEnvironment() from jinja2.sandbox.",
        "Step 3: Verify jinja2>=2.10 is pinned in requirements.txt (SandboxedEnvironment is included by default).",
        "Step 4: Add a test that passes {{ ''.__class__.__mro__[1].__subclasses__() }} as a template and asserts SecurityError is raised."
      ],
      "testingHint": "Send payload `{{ 7*7 }}` — result must be literal string, not `49`. Send `{{ ''.__class__ }}` — must raise SecurityError or return empty.",
      "secretsManagement": null,
      "suppressionAnnotation": null
    }
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
- Never treat chat JSON output as a substitute for the required database writes.
- Never provide abstract PoC text without concrete executable steps.
- Never return `reasoning` as unstructured plain text.
