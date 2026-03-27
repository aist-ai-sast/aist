---
name: context-extractor-mcp-audit
description: Replay real findings through the live context-extractor-mcp AI-triage flow, validate every applicable MCP tool, and convert confirmed bugs or uncovered valid scenarios into isolated tests. Use when auditing all findings or batches of findings for one or more pipeline ids, defaulting to 69ec5b01, 07734951, 9ce90895, and 5a36b942.
---

# Inputs

- `pipeline_ids` (optional): List of pipeline ids to process. Default:
  - `69ec5b01`
  - `07734951`
  - `9ce90895`
  - `5a36b942`
  These pipeline ids are project entry points. For each id, resolve `pipeline.project` and audit all findings belonging to that project. Do not limit the audit to findings produced only by that specific pipeline run.
- `batch_size` (optional): Findings per pass. Default `60-100`.
- `progress_log` (optional): Progress file path. Default:
  - `sast-combinator/context_extractor_service/ansible/files/tests/context_extractor_mcp_audit_progress.jsonl`

# Core Rule

This skill is live-triage-first, not synthetic-test-first.

Do not begin by inventing fixtures or synthetic tests. First replay each real finding exactly as an AI triage agent would use MCP according to:

- `aist/ai_triage_system_prompt.md`

Only after a real finding has been processed through the live MCP flow may you derive a minimal isolated fixture and add or update tests.

Every new test must come from a real finding that was first validated through the live agent flow.

Do not stop at the cheapest subset of tools. The goal is not to get a minimal partial answer. The goal is to validate the full relevant AI-triage tail for each finding, especially the higher-value flow and navigation tools when they are applicable.

This audit must remain manual at the decision layer. Large volume changes batching and bookkeeping, but it does not permit replacing per-finding triage with a newly invented automated auditor.

# Oracle Priority

When deciding what a test should assert, use this priority order:

1. real source semantics from the live finding;
2. documented MCP tool contract;
3. the isolated fixture that preserves the same semantics;
4. the currently observed MCP output.

Observed MCP output is evidence of current behavior, not the default source of truth for expected behavior.
If observed MCP output disagrees with source-derived expected behavior, preserve the source-derived expectation and treat the mismatch as a bug.

# Hard Constraints

1. Process the full finding set for the projects resolved from the selected pipeline ids. The skill is not complete until all findings in those projects are handled.
2. Work in batches of `60-100` findings, then continue with the next unprocessed batch.
3. Do not modify `context_extractor` source code.
4. Do not rebuild or restart the existing container.
5. Use the already running environment.
6. Do not use `sast-combinator/context_extractor_service/ansible/files/tests/live_mcp_group_audit.py`.
7. Final tests and fixtures must not depend on live `/tmp/aist/projects/dev/...` paths.
8. Keep tests isolated, minimal, and semantically faithful to the original real finding.
9. Avoid duplicate coverage by checking the existing suite first.
10. Do not build a new auditor, replay engine, scoring layer, classifier, or automatic verdict framework to replace manual finding-by-finding triage.
11. Do not stop after processing only 1-2 findings unless a real blocker prevents further work.
12. Any test reproducer must be bounded and CI-safe. Preserve the real triggering structure, but do not create fixtures whose size or runtime is uncontrolled.

# Autonomy And Reporting Rule

This skill is intended for long autonomous runs.

Do not pause for user input after each finding or after a very small number of findings.

Default operating behavior:

1. Continue working without asking for confirmation between findings.
2. Continue across multiple batches unless blocked by a real hard blocker.
3. Give progress summaries only at meaningful milestones, not after every small step.

Default reporting interval:

- report after every `200` processed findings; or
- report earlier only if a real blocker appears that prevents continued work; or
- report earlier only if a major new bug pattern is discovered that materially changes the audit strategy.

Prohibited behavior:

- stopping after 1-2 findings just to provide a partial summary;
- asking the user whether to continue when no blocker exists;
- treating small progress updates as a reason to hand control back to the user.

If there is no blocker, keep processing findings until the next reporting milestone is reached.

# Manual Triage Rule

This audit must be driven by manual finding-by-finding triage, even when the total volume is large.

The agent must manually evaluate each finding one by one.

Allowed automation:

- fetching batches of findings from the DB or container;
- writing and updating the progress log;
- simple helpers for opening files, grouping work, or resuming from the next unprocessed finding;
- simple helpers that collect raw MCP outputs without interpreting them.

Prohibited automation:

- any script or tool that decides whether a finding is covered, valid, buggy, or not applicable;
- any script or tool that decides expected MCP outputs instead of the agent verifying them from sources;
- any script or tool that replaces manual replay of the AI triage flow;
- any new audit framework that itself would require separate trust-building or validation before it could be relied on.

The scale of the task changes scheduling, not judgment. Judgment must remain manual per finding.

Manual triage does not mean frequent user interaction. The agent must keep making per-finding judgments autonomously and continue working until the next reporting milestone or a real blocker.

# Bootstrap Rule

If the progress log does not exist yet, start with a bootstrap pass.

Bootstrap pass goals:

- inspect the existing tests and fixtures;
- identify already covered finding patterns at a practical level;
- create the progress log;
- then begin normal finding-by-finding manual triage.

Bootstrap pass limits:

- it is not a substitute for manual triage of real findings;
- it must not become a separate coverage-analysis project;
- it must only gather enough context to reduce duplicate test creation and enable safe resumption.

Do not attempt to pre-classify all findings as covered or uncovered before manual triage.

Coverage decisions for individual findings must still be made during normal finding-by-finding processing.

Bootstrap pass should be short and operational. Do not return to the user after bootstrap alone unless blocked.

# Scope Semantics

The provided `pipeline_ids` are project entry points, not a filter over findings by pipeline run.

For each provided `pipeline_id`:

1. Resolve the corresponding `pipeline.project`.
2. Enumerate all findings belonging to that project.
3. Process the full finding set of that project.

Wrong interpretation:

- process only findings directly attached to the listed pipeline runs.

Correct interpretation:

- use the listed pipeline ids to find the target projects, then audit all findings in those projects.

# Required Processing Order

For each provided pipeline id:

1. Load the pipeline by `pipeline_id`.
2. Resolve `pipeline.project`.
3. Enumerate all findings belonging to that project.
4. Process that project finding set in batches until complete, continuing autonomously until the next reporting milestone or a real blocker.

For each finding in the resolved project scope:

1. Load the real finding from the DB/container dataset.
2. Determine whether it follows the `code` branch or the `config` branch.
3. Replay the full relevant AI triage flow from `aist/ai_triage_system_prompt.md` against the live MCP behavior.
4. Record the result of every relevant MCP tool.
5. Compare the live MCP output with the expected result inferred from the real project sources under `/tmp/aist/projects/dev/...`.
6. Manually decide whether the finding is:
   - already covered by existing isolated tests;
   - a new valid uncovered scenario;
   - a confirmed MCP bug.
7. Only then, if needed, create a minimal isolated fixture and add or update tests derived from that finding.

The agent must personally perform the applicability decision, source inspection, expected-vs-actual comparison, and final triage decision for every finding.

Do not surface an interim summary to the user for isolated findings unless the reporting milestone has been reached or continued work is blocked.

# Required MCP Tool Coverage

A finding is not processed until every relevant MCP tool has an explicit status for that specific finding.

Simple early tools such as `classify_file`, `extract_function`, `find_imports`, or `find_decorators` are never sufficient on their own when deeper flow/navigation tools are applicable.

If the finding shape makes deeper tools applicable, the audit must continue through them. Do not stop early just because the first tools returned plausible results.

Per-tool statuses:

- `applicable_pass`
- `applicable_fail`
- `applicable_exception`
- `not_applicable`

## Code Findings

Check all applicable tools:

- `classify_file`
- `extract_function`
- `find_imports`
- `find_decorators`
- `find_identifiers`
- `trace_identifier_backward`
- `find_callers`
- `find_definition`
- `find_route_to_function`

For code findings, the highest-value tools are usually:

- `find_identifiers`
- `trace_identifier_backward`
- `find_callers`
- `find_definition`
- `find_route_to_function`

These tools must be treated as mandatory whenever the finding shape makes them applicable. A code finding is not complete if the audit stopped after only classification/extraction/import/decorator checks.

## Config Findings

Check all applicable tools:

- `classify_file`
- `classify_environment`
- `extract_config_block`
- `extract_env_variables`
- `find_related_configs`

# Tool Validation Rules

1. If a tool is applicable for the finding shape, call it and validate the result.
2. If a tool is applicable and MCP returns the wrong value, that is a bug.
3. If a tool is applicable and MCP raises an exception, that is a bug.
4. If a tool is not applicable for the finding shape, record `not_applicable` explicitly.
5. A finding cannot be marked as processed until all relevant tools have statuses.
6. Existing coverage still requires full live-triage replay and per-tool status recording before the finding can be marked complete.
7. For code findings, do not mark the finding complete if only the shallow tools were evaluated while any of `find_identifiers`, `trace_identifier_backward`, `find_callers`, `find_definition`, or `find_route_to_function` remain unverified and applicable.
8. Treat unjustified early stopping as an audit failure, not as a completed finding.
9. If a tool runs so long that it is reasonably likely to be hung, deadlocked, or otherwise not making progress, treat that behavior as a confirmed bug candidate, capture the concrete reproducing situation, and convert it into an isolated test case.

# Depth-First Audit Rule

When in doubt, bias toward checking the deeper tools, not skipping them.

Required behavior:

1. Start with the early tools from the AI triage flow.
2. Continue into the deeper flow/navigation tools whenever the finding shape permits it.
3. Explicitly justify every `not_applicable` status for the deeper tools in the log note or in the test comments when the reason is not obvious from the finding shape.

Prohibited behavior:

- stopping after `classify_file` or `extract_function` because they already look correct;
- treating a finding as complete after only the cheap tools;
- skipping `trace_identifier_backward`, `find_callers`, `find_definition`, or `find_route_to_function` only because they are slower or harder to validate.

# Test Authoring Rules

1. Do not add tests that were not first motivated by a real processed finding.
2. Derive minimal isolated fixtures from the real finding only after live replay is complete.
3. Do not over-minimize a fixture in a way that changes the semantics of the original finding.
4. Final assertions must be valid for the isolated fixture, including exact lines, snippets, identifiers, traces, callers, definitions, routes, config blocks, and environment expectations where applicable.
5. Add tests under:
   - `sast-combinator/context_extractor_service/ansible/files/tests`
6. Add fixtures under the appropriate tests fixture directory.
7. Prefer existing thematic test files, but do not let them grow into huge files.
8. If no suitable small file exists, create a new small thematic test file.
9. When a bug is a tool that hangs or runs unreasonably long, add a reproduction-oriented test case for the exact triggering scenario, not just a prose note.
10. Anonymize any strings in tests, fixtures, comments, or `xfail` reasons that look like secrets, tokens, passwords, private keys, credentials, or other sensitive values while preserving the behavior needed to reproduce the issue.
11. If a bug only becomes visible on a deep, wide, or otherwise stress-heavy source shape, keep that shape in the fixture. Do not "simplify" away the nesting, repetition, or size if those properties are part of the real reproducer.
12. Deep-nested, loop-heavy, or performance-stress fixtures are allowed when they come from a real finding or from a manually validated reduction of a real reproducer. They are not allowed as purely invented synthetic stress tests with no demonstrated link to a real source shape.
13. Any test that patches module-global state in `mcp_server` or `context_extractor.*` must use `monkeypatch.setattr(...)` or an equivalent automatically reverted mechanism. Do not use direct module-level assignment that can leak state into later tests.
14. When deciding whether an existing test already covers a finding, require all three:
    - same MCP tool;
    - same language or construct family;
    - same failure mode or semantic expectation.
    Similar topic alone is not enough to mark a finding as covered.
15. Prefer semantic fidelity over minimality. If there is a conflict:
    - semantic fidelity to the real finding wins first;
    - reproducer stability and bounded execution win second;
    - fixture minimality wins last.

## Line Number Contract

**All MCP tool line numbers are 1-indexed.** Line 1 is the first line of the file.

## Mandatory Line-Selection Workflow

For every new or updated regression test that depends on a specific line, the agent must complete all three steps below in order.

The agent must leave an explicit audit trail for this workflow in either:

- the progress log entry for the finding; or
- a short test docstring/comment while the test is being authored.

That record must include:

- live source line number;
- live source line text;
- fixture line number;
- tool under test.

### Step A. Capture the source-of-truth line from the real finding

Before deriving an isolated fixture, record from the live project source:

- the exact source line text the finding is about;
- the exact 1-indexed line number used for the live MCP replay;
- which tool output is expected to describe that line.

Do not proceed if the "real finding line" is ambiguous. Resolve that ambiguity first by inspecting the live source and the finding context.

### Step B. Preserve that same line in the isolated fixture

After creating the isolated fixture, verify that the fixture still contains the same semantic target line as the real source.

You must explicitly confirm:

- the exact fixture line number;
- the exact `code_on_line` or raw source line text at that line;
- that this fixture line is the same semantic target as in Step A, not a neighboring control-flow line or body line.

If blank lines, multiline constructs, or fixture reduction changed the target line, update the fixture or update the selected line number before writing assertions.

### Step C. Write assertions only for that verified target line

All assertions for `extract_function`, `find_identifiers`, and `trace_identifier_backward` must be written against the single verified target line from Step B.

Prohibited:

- calling a tool on line N while asserting semantics from line N+1 or N-1;
- calling a tool on a control-flow line and asserting payload from the body line;
- calling a tool on an assignment line and asserting payload from the surrounding `with`, `if`, `for`, or `try`;
- changing assertions to match a different line instead of fixing the selected line number or fixture.

If the isolated fixture cannot preserve the real finding's target line cleanly, the fixture is not ready yet and must be revised before assertions are written.

## Verify Line Selection And Reproduce Current Behavior

Before finalizing any line-sensitive assertion, call the tool directly in the container to:

1. confirm that the selected fixture line is the intended semantic target;
2. reproduce the current MCP behavior on that exact line.

Use this step to validate line selection and capture current behavior.
Do not use it as permission to rewrite expected values to match semantically wrong MCP output.

Example:

```python
docker run --rm -w /app aist-context-extractor-mcp:test python -c "
import mcp_server
from pathlib import Path
import tempfile

source = '''...paste fixture source here...'''
tmpdir = Path(tempfile.mkdtemp())
file_path = 'fixture.ext'
(tmpdir / file_path).write_text(source)
mcp_server._resolve_source_dir = lambda _pid: tmpdir

# Find the right line number by scanning
for ln in range(1, len(source.splitlines()) + 2):
    r = mcp_server.extract_function('test', file_path, ln)
    col = r['meta'].get('code_on_line', '')
    if col:
        print(f'  Line {ln}: {repr(col)}')

# Then verify identifiers and trace for the correct line
ids = mcp_server.find_identifiers('test', file_path, CORRECT_LINE)
print('identifiers:', ids)
trace = mcp_server.trace_identifier_backward('test', file_path, CORRECT_LINE, 'IDENTIFIER')
print('trace:', trace)
" 2>&1 | grep -v INFO
```

**Rules:**
- Never guess line numbers from counting the fixture source manually — always verify with the tool.
- The SAST scanner may report findings with 0-indexed or 1-indexed lines depending on the tool; always check which convention was used and convert to 1-indexed for MCP calls.
- `trace_identifier_backward` returns `"line"` as 1-indexed (= internal 0-indexed stmt_line + 1). The condition `stmt_line >= line_number` with 0-indexed stmt_line and 1-indexed line_number means the trace includes the line where the call is made (because 0-indexed N-1 < 1-indexed N). Account for this when writing trace assertions.
- If `code_on_line` is empty or wrong, the line number is almost certainly wrong — do not adjust the assertion, adjust the line number.
- `find_identifiers` and `trace_identifier_backward` are usually line-local in what they report, but expected results must be derived from the source and the intended tool contract, not copied from the current MCP output.
- First decide which concrete source line the real finding is about. Then derive the semantically correct expectation for that exact line from the fixture source. Only after that compare MCP output to the expected result.
- Do not mix expectations from a control-flow line and its body unless the intended contract for that tool explicitly says both should appear. If the real finding is on `with open(...)`, validate the semantics of that line; if it is on `data = f.read()`, validate that line instead.
- Re-run the exact MCP call on the isolated fixture before finalizing the test, but use that rerun only to confirm line selection and reproduce the current behavior. Do not blindly rewrite assertions to match the observed MCP output if the output is semantically wrong for the selected line.
- If MCP output and source-derived expectation disagree, preserve the source-derived expectation and record it as a normal failing assertion or `xfail`, depending on whether the bug is being fixed now or intentionally deferred.

# Bug Reproduction Rules

Use `pytest.mark.xfail(strict=True)` only for confirmed MCP defects discovered through live finding replay.

Each `xfail` reason must include:

- pipeline id;
- file path;
- line number;
- failing tool;
- short mismatch summary.

If the defect is a hang or excessive runtime, the mismatch summary must say that explicitly and the test must preserve the concrete reproducing structure that caused the stall.

For hang, deadlock, or pathological-runtime bugs:

- preserve the relevant structural property from the real reproducer, such as deep nesting, chained callbacks, large literal expansion, or repeated wrapper nodes;
- do not replace a real stress reproducer with a smaller fixture unless you have manually proven that the smaller fixture still reproduces the same bug;
- explicitly note in the test docstring or comment which structural property is essential to the reproduction.
- keep the reproducer bounded enough that the test can complete under CI when the bug is fixed;
- if the reproducer must remain expensive, prefer an `xfail(strict=True)` or another bounded regression pattern over an effectively unbounded normal test.

# Progress Log

Maintain a persistent progress log so the audit can resume safely and so findings are not processed in a loop.

Create the progress log in the tests directory unless the caller explicitly overrides it.

Use the schema in:

- [references/progress-log-format.md](references/progress-log-format.md)

# Definition Of Done

The task is complete only when:

1. All findings from the full dataset for the projects resolved from the selected pipeline ids are processed, not just sampled.
2. Every finding has a progress-log entry.
3. Every relevant MCP tool has an explicit status for every finding.
4. Every confirmed bug found through live replay has an isolated `xfail` reproduction.
5. Every valid uncovered real scenario has an isolated normal test.
6. Final tests and fixtures are independent from live `/tmp/aist/projects/dev/...` paths.
7. New tests pass syntax validation.
8. If feasible without changing the environment, the relevant test subset is executed.
9. The agent did not require unnecessary user interaction between normal batches.
