# aist-debug

Systematic root-cause debugging for any AIST component regression.
NO FIX WITHOUT ROOT CAUSE FIRST.

Use for regressions in: `aist/` API, Celery tasks, deduplication, sast-pipeline,
client-ui data issues. For MCP server regressions use `/mcp-regression-fix` instead.

## Inputs

- `symptom` (required): what broke — error message, wrong behavior, failing test name
- `component` (optional): `api`, `celery`, `dedupe`, `pipeline`, `ui`

## Phase 1 — Reproduce

Before reading any code, reproduce the failure:

1. If there is a failing test: run it and capture the full output including traceback.
2. If no test: write the minimal reproducer first (curl command, Django shell snippet,
   or test case) that demonstrates the wrong behavior.
3. Confirm the reproducer is stable (fails consistently, not flaky).

**Stop if you cannot reproduce.** Do not proceed to Phase 2 without a stable reproducer.

Write down: "The failure is: <exact error or wrong value>. Reproduced via: <command>."

## Phase 2 — Trace to root cause

Follow the call chain from symptom to origin. Do NOT read all files — follow the chain.

**By component:**

| Component | Start here | Follow to |
|---|---|---|
| `aist/` API | Request → ViewSet → `get_queryset()` → serializer | Model, queryset filter |
| Celery task | Task function → called service → model operation | DB state, task args |
| Dedup | `aist/dedupe/` → hash computation → DB comparison | Model field, hash inputs |
| sast-pipeline | `run_pipeline.py` → builder/analyzer → REST client | Docker output, API call |
| client-ui | API call → response payload → component render | Serializer field, API filter |

At each step ask: "Is the input to this function correct? Is the output correct?"
Stop at the first function where input is correct but output is wrong — that is the root cause.

**Red flags — stop and reconsider if:**
- You are about to make a change without pinpointing the exact wrong line
- You are combining multiple changes to "try something"
- You have made 3+ changes without the reproducer passing

Write down: "Root cause is <function> in <file>:<line> because <exact reason>."

## Phase 3 — Verify hypothesis with minimal change

Before fixing, confirm the hypothesis:
- Add a temporary `print`/`logger.debug` or assertion to confirm the wrong value
- Run the reproducer again to see the log

If the log confirms the hypothesis: proceed to Phase 4.
If not: return to Phase 2 — the root cause is elsewhere.

## Phase 4 — Fix

Write the minimal fix that addresses ONLY the root cause. Rules:

- Change the fewest lines possible
- Do NOT refactor surrounding code as part of the fix
- Do NOT combine multiple fixes in one commit
- If the fix requires a Django migration: treat it as a separate step (see `/mcp-regression-fix` for migration patterns or `aist-migration-validator` for safety checks)

## Phase 5 — Write or update the test

The fix is not complete until a regression test exists:

- If there was already a failing test: confirm it now passes
- If there was no test: write one that would have caught this regression
- Test must reflect the real scenario (user action or data flow), not just call the fixed function

For `aist/` API regressions: test via DRF APIClient, not by calling the view directly.
For Celery regressions: test the task function with realistic arguments.
For dedup regressions: test with two findings that should/should not be considered duplicates.

## Phase 6 — Verify nothing else broke

Run the narrowest test suite that covers the changed area:

```
# API change
docker compose --env-file .env.dev exec app \
  python manage.py test aist.test.<relevant_module> -v 2

# Dedup change
docker compose --env-file .env.dev exec app \
  python manage.py test aist.test.dedupe -v 2
```

Do NOT run the full suite unless the change is cross-cutting.

## Done when

- [ ] Reproducer passes (or test passes)
- [ ] Root cause named explicitly (file + line + reason)
- [ ] Fix is minimal — no unrelated changes
- [ ] Regression test added or updated
- [ ] Narrowest relevant test suite passes
- [ ] ruff passes on changed files

## How to trigger

```
/aist-debug symptom="GET /api/findings/ returns findings from other orgs"
/aist-debug symptom="Celery task aist.tasks.run_pipeline fails with KeyError: pipeline_id" component=celery
/aist-debug symptom="Duplicate findings not being deduplicated after model field rename" component=dedupe
```
