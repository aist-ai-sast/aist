---
name: aist-jira-description
description: Generate grouped Jira ticket descriptions from AIST findings by clustering related issues into remediation families and splitting them by project-specific source path.
---

# aist-jira-description

Generate Jira ticket descriptions from AIST findings, which don't have connected work item link.
Findings are semantically grouped into remediation families; each family becomes one ticket.
Within each ticket, findings are subdivided by project path prefix.
Each Jira row must be developer-ready, line-accurate, and tied to an immutable source revision.

This skill is not a generic summarizer. The output must tell a developer:
- what is actually wrong
- why it is exploitable
- which exact code location needs to change
- what fix pattern applies in this codebase

Do not stop after first-pass generation. This skill includes a mandatory validation pass over the generated markdown.

## Inputs

- `project_name` (required): AIST project name; source tree is under `/tmp/aist/projects/dev/<project_name>/`
- `severity` (optional): `critical`, `high`, `medium`, `low`, `any` (default: `any`)
- `output_dir` (optional): directory to save generated `.md` files (default: triage/jira_<project_name>)
- Optional local source checkout path for a specific run, if the user provides it. Prefer this over a generic repo checkout.

## Step 1 — Fetch findings and project-version metadata

Query active true-positive findings for the project,which don't have related work item link, including the linked `project_version`.
You need the project version commit hash to build immutable source links.

```python
docker compose exec uwsgi python manage.py shell -c "
from aist.models import AISTProject
from dojo.models import Finding

project = AISTProject.objects.get(name='<project_name>')
versions = list(project.aistprojectversion_set.all())
engagement_to_version = {v.engagement_id: v for v in versions}
engagements = [v.engagement_id for v in versions]

findings = Finding.objects.filter(
    test__engagement_id__in=engagements,
    active=True,
    false_p=False,
    work_item_links__isnull=True,
    ).distinct().order_by('file_path', 'line', 'id')

for fn in findings:
    version = engagement_to_version.get(fn.test.engagement_id)
    print('|'.join([
        str(fn.id),
        fn.severity or '',
        fn.title or '',
        str(fn.cwe or ''),
        fn.vuln_id_from_tool or '',
        fn.file_path or '',
        str(fn.line or ''),
        fn.sourcefile_link or '',
        version.version if version and version.version else '',
        str(version.id) if version else '',
    ]))
"
```

Filter by `severity` input if provided.

Retain at least these fields per finding:
- `finding.id`
- `finding.file_path`
- `finding.line`
- `finding.sourcefile_link`
- `project_version.id`
- `project_version.version`

`project_version.version` is the canonical git commit hash for the source link.
Do not replace it with a branch name.

## Step 2 — Semantic grouping

AIST has no built-in grouping. Group findings yourself using the following signals, in priority order:

**Signal 1 — `vuln_id_from_tool`** (strongest)
Findings with the same SAST rule ID usually describe the same vulnerability family.

**Signal 2 — CWE**
Use this when `vuln_id_from_tool` is absent or too granular.

**Signal 3 — Normalized title**
Strip finding-specific tokens such as file names, function names, variables, line numbers, and service names.

Examples:
- "Missing USER directive in Dockerfile" → `Missing USER directive`
- "SQL injection in views.py:142 via user_id" → `SQL injection`
- "Hardcoded password in config/settings.py" → `Hardcoded password`

**Signal 4 — File pattern + title combination**
When needed, combine:
- file type or path pattern
- vulnerability category inferred from the title

Produce a list of groups, each with:
- `family_name`
- `findings`
- `grouping_rationale`

Each finding belongs to exactly one family. If a finding truly does not fit, place it in `Miscellaneous`.

## Step 3 — Row-level deduplication

After semantic grouping, build Jira rows by remediation family, not by raw finding.

Goal:
- one row per dangerous sink or fix family
- not one row per scanner output

Merge findings into one row when all of the following are true:
- they point to the same dangerous sink or the same effective code construct
- the exploitability reasoning is the same
- the developer fix is materially the same

Examples that should usually be merged:
- the same sink repeated across historical commits
- several findings in one file that are all addressed by one code change
- adjacent unsafe HTML construction lines that share one fix family
- framework wrappers that all implement the same insecure default pattern

When merged:
- keep all relevant `Location` links in one cell, comma-separated
- keep all `SAST URL` links in one cell, comma-separated
- write one shared remediation for that fix family

Do not keep separate rows only because:
- the commit differs
- the line differs slightly
- the finding came from a different run

Keep separate rows when:
- the sink is materially different
- the fix differs
- one case is conditional or weaker than the others

## Step 4 — Read local source and identify the real sink

Prefer the most specific local source checkout available.

Typical options:
- generic project root: `/tmp/aist/projects/dev/<project_name>/`
- run-specific checkout provided by the user, for example:
  `/tmp/aist/projects/dev/<project_name>/develop/runs/<pipeline_id>/dev_<project_name>`

Use the local checkout to inspect the real code. Do not rely on remote provider rendering to infer lines.

For each finding:
- locate the file locally
- inspect the reported line
- expand outward until the context is clear
- identify the real dangerous sink or construct requiring a fix

You must determine:
- whether the finding line is accurate
- whether a nearby line is more useful for the developer
- whether the finding is a duplicate of another row
- whether the case is strong, conditional, or likely false positive

Do not blindly trust `finding.line`.
The final Jira link must point to the actual dangerous moment, not a nearby helper, blank line, listener registration, or wrapper code.

If the file cannot be read:
- fall back to finding metadata
- explicitly note the fallback in the row or surrounding text

## Step 5 — Build immutable source links

Every `Location` entry must use an immutable commit-based blob URL.

Rules:
- Never use a branch name, tag, or any floating ref in `blob/...`
- Always use `project_version.version` as the blob ref for that finding
- Always include `#L<line>`
- Preserve the integration provider host/path from `sourcefile_link` when available

Correct pattern:
- `https://<provider>/<org>/<repo>/-/blob/<commit_hash>/<path>#L<line>`

Incorrect patterns:
- `.../blob/develop/...`
- `.../blob/main/...`
- any URL without a line anchor when a specific line is known

If a merged row contains findings from different commits:
- include multiple immutable blob links in the same cell
- do not collapse them into one floating ref

If the provider URL exists in `sourcefile_link`:
- keep that provider host/path
- replace only the blob ref and line anchor as needed

## Step 6 — Write developer-ready ticket sections

### Description

`Description` must explain what the developer needs to fix.

It should:
- describe the vulnerable pattern and trust boundary
- describe what class of code change is expected

It must not:
- explain why findings were grouped together
- contain grouping meta-rationale
- read like scanner output

### Impact

State the concrete impact. Avoid overclaiming.

If the case is conditional or depends on downstream validation, say so explicitly.

### Why This Is Exploitable

Every ticket must include a `Why This Is Exploitable` section.

This section is mandatory and must be concrete:
- where attacker control comes from
- how it reaches the sink
- what action, execution, or state change it enables
- what preconditions are required

Good examples:
- stored data flows into `innerHTML` and executes in the admin origin
- popup message handlers trust `event.data` without validating `origin` or `source`
- the issue is exploitable when callers rely on insecure library defaults

Conditional wording is required when appropriate, for example:
- "This risk depends on whether Cloud DB validates `redirect_uri` for the supplied client."
- "This is an insecure-defaults issue that becomes exploitable when integrators rely on the built-in transport defaults."

### Expected Fix

`Expected Fix` is the family-level remediation pattern.

It must:
- match the real architecture in the codebase
- include a short representative code example
- mention variants if different wrappers/frameworks need slightly different fixes

Do not use fake simplifications that would not really work in this codebase.

### Suggested remediation per row

Each row must include a concrete, applicable remediation with a short example.

It must:
- name the exact construct to change
- state the concrete action
- be implementable by a developer unfamiliar with the area

It must not:
- be generic boilerplate
- suggest an unrealistic fix
- overstate certainty for a conditional case

## Step 7 — Compose the Jira description

For each finding family, generate one `.md` file with this structure:

```markdown
## Summary

<one line: "Remediate <vulnerability type> in <project_name>">

## Description

<2-3 sentences: what the problem is and what developers need to change>

<bulleted general remediation approach>

## Impact

<2-4 sentences: concrete impact, with careful wording if conditional>

## Why This Is Exploitable

<2-5 sentences: concrete exploit path, attacker control, sink, and preconditions>

## Expected Fix

<general fix pattern with representative code snippet(s)>

## Affected findings

### <project/path/prefix>

| Location | Suggested remediation | SAST URL |
|---|---|---|
| [path/to/File@<commit>#LNN](<provider_blob_url_with_commit_hash>) | <specific developer-ready fix with example> | [<id>](https://aist.itsec-europe.com/findings/<id>) |
```

Project path prefix:
- use the longest common directory prefix when it is meaningful
- if it is too broad, use the first 2-3 path segments

Multiple findings in one row:
- keep all relevant `Location` links in the same cell, comma-separated
- keep all `SAST URL` links in the same cell, comma-separated
- do not drop line anchors just because they differ

## Step 8 — Jira markdown compatibility rules

Output must be Jira-friendly markdown.

Rules:
- Do not use HTML such as `<br>` inside table cells
- Keep tables as plain markdown tables
- Ensure inline code examples have balanced quotes
- Keep `SAST URL` in every row
- Keep text concise enough to remain readable in Jira tables

## Step 9 — Save output

- File name: `jira_<family_slug>_<project_name>.md`
- `family_slug` = lowercased family name with spaces/special chars replaced by `_`, max 40 chars
- Save to `output_dir`
- After saving, print the full content of each file to stdout

If multiple families were processed, also print an index:

```text
Generated N Jira descriptions for project '<project_name>':
1. jira_docker_root_<project_name>.md — "Remediate Docker images running as root" (12 findings, 4 path groups)
2. jira_sql_injection_<project_name>.md — "Remediate SQL injection" (3 findings, 2 path groups)
```

## Step 10 — Mandatory validation pass

Re-open the generated markdown and validate it against local source before finalizing.

For every row:
- inspect each linked file and line locally
- confirm the line points to the actual dangerous sink or construct requiring a fix
- move the line anchor if a nearby line is more accurate for the developer

Typical mistakes that must be corrected:
- line points to listener registration instead of unsafe message handling
- line points to helper definition instead of the redirect sink
- line points to a blank line or container instead of `innerHTML`, `redirect(...)`, `verify=False`, etc.
- duplicate rows that should be one remediation-family row
- wording that overstates certainty for a conditional case

Do not skip this pass.

## Step 11 — Self-check before finalizing

- [ ] Every fetched finding appears in exactly one Jira row
- [ ] `family_name` reflects the remediation family clearly
- [ ] Duplicate findings across commits/runs were merged when the sink and fix are the same
- [ ] Separate rows remain only where sink or fix really differs
- [ ] `Description` explains what to fix, not why grouping happened
- [ ] `Why This Is Exploitable` is present and concrete
- [ ] `Impact` wording matches the certainty level
- [ ] Specific remediations are actionable, not generic
- [ ] Row-level remediation examples are applicable to this codebase
- [ ] Source file was actually read for each finding, or fallback was explicitly noted
- [ ] Every `Location` link uses an immutable commit hash, never a branch ref
- [ ] Every `Location` link has a line anchor
- [ ] Every line anchor was validated against local source
- [ ] Provider blob URLs preserve the integration host/path when available
- [ ] No `<br>` or broken markdown/quote formatting remains
- [ ] `SAST URL` is present in every row
- [ ] AIST finding URLs use correct IDs
- [ ] All output files are `.md`

## How to trigger

```bash
/aist-jira-description project_name=nx
/aist-jira-description project_name=nx severity=critical
/aist-jira-description project_name=nx severity=high output_dir=docs/jira
```
