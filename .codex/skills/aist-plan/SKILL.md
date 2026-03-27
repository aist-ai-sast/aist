# aist-plan

Create an atomic implementation plan before starting any non-trivial feature or refactor.
Decompose the work into 2-5 minute tasks, each with a failing test first.
Prevents rushing into implementation without a clear scope.

## When to use

Use before implementing any feature, refactor, or change that touches more than one file
or requires more than one logical step. Skip for trivial single-line fixes.

## Inputs

- `feature` (required): short description of what needs to be done
- `context` (optional): relevant files, constraints, or background

## Step 1 — Explore before planning

Do NOT write any code or plan yet. First:

1. Read the relevant files to understand current state:
   - If touching `aist/api/`: read the relevant api file and a neighboring one for patterns
   - If touching `context_extractor_service/`: read the relevant module and its tests
   - If touching `sast-pipeline/`: read `analyzers.yaml` and the relevant pipeline module
2. Run `git log --oneline -10` to see recent changes in the area
3. Grep for existing tests covering the area to understand what's already tested

Write a one-paragraph summary: "Current state is X. The change needs to Y. Risk areas are Z."

## Step 2 — Identify atomic tasks

Break the feature into tasks where each task:
- Takes 2-5 minutes to implement
- Has exactly one testable outcome
- Can be committed independently
- Follows test-first: failing test → implementation → passing test

**Task format:**
```
### Task N: <imperative title>

**Test first:** Write a test that asserts <specific observable outcome>.
Expected failure: <what error the test produces before implementation>.

**Implementation:** <what code to write, which file, which function>.

**Verify:** Run `<specific test command>`. All N tests pass.

**Commit:** `<commit message>`
```

**Rules for decomposition:**
- Each task touches ideally one file (two maximum)
- If a task requires more than ~20 lines of new code, split it
- Database migration = its own task
- Test fixture creation = its own task if complex
- API endpoint = its own task, separate from business logic

## Step 3 — Security and pattern check

Before finalizing the plan, verify:
- [ ] Any new QuerySet in `aist/` will have org filter — mark which task adds it
- [ ] Any new MCP file-reading code will have path guard — mark which task adds it
- [ ] Any new API endpoint has `permission_classes` — mark which task adds it

If a task would create a security gap that a later task fixes — merge them into one task.

## Step 4 — Write and save the plan

Save to `docs/plans/YYYY-MM-DD-<feature-slug>.md`:

```markdown
# Plan: <feature>

**Date:** YYYY-MM-DD
**Context:** <one-paragraph summary from Step 1>
**Estimated tasks:** N

## Tasks

### Task 1: ...
### Task 2: ...
...

## Open questions
- <anything unclear that needs decision before starting>
```

Present the plan to the user. Do NOT start implementing until explicitly asked.

## Step 5 — Raise open questions

Before implementation begins, list any decision points:
- Ambiguous requirements
- Trade-offs between approaches
- Dependencies on other in-progress work
- Security implications that need design decision

## How to trigger

```
/aist-plan feature="Add pipeline status endpoint to REST API"
/aist-plan feature="Refactor dedup hash computation" context="aist/dedupe/, aist/models.py"
```
