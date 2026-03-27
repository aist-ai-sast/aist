---
name: aist-jira-description
description: Generate grouped Jira ticket descriptions from AIST findings by clustering related issues into remediation families and splitting them by project-specific source path.
---

# aist-jira-description

Generate Jira ticket descriptions from AIST findings.
Findings are semantically grouped into families; each family becomes one ticket.
Within each ticket, findings are subdivided by project (source path prefix).
Each finding gets a specific, actionable remediation based on its actual source code.

## Inputs

- `project_name` (required): AIST project name; source tree is at
  `/tmp/aist/projects/dev/<project_name>/`
- `severity` (optional): `critical`, `high`, `medium`, `low`, `any` (default: `high`)
- `output_dir` (optional): directory to save generated `.md` files (default: current dir)

## Step 1 — Fetch findings

Query active true-positive findings for the project:

```python
docker compose exec app python manage.py shell -c "
from aist.models import AISTProject
from dojo.models import Finding
project = AISTProject.objects.get(name='<project_name>')
engagements = project.aistprojectversion_set.values_list(
    'engagement_id', flat=True)
findings = Finding.objects.filter(
    test__engagement_id__in=engagements,
    active=True,
    false_p=False,
).order_by('file_path')
for fn in findings:
    print('|'.join([
        str(fn.id),
        fn.severity or '',
        fn.title or '',
        str(fn.cwe or ''),
        fn.vuln_id_from_tool or '',
        fn.file_path or '',
        str(fn.line or ''),
        fn.sourcefile_link or '',
    ]))
"
```

Filter by `severity` input if provided.

## Step 2 — Semantic grouping

AIST has no built-in grouping. Group findings yourself using the following signals,
in priority order:

**Signal 1 — `vuln_id_from_tool`** (strongest)
Findings with the same SAST rule ID (e.g., same semgrep rule, same CodeQL query,
same cppcheck ID) almost certainly describe the same vulnerability family.
Group them together.

**Signal 2 — CWE**
Findings with the same CWE number belong to the same weakness class.
Use this when `vuln_id_from_tool` is absent or too granular (many different rule IDs
all mapping to CWE-89 → one SQL injection group).

**Signal 3 — Normalized title**
Strip finding-specific tokens from the title: file names, function names, variable names,
line numbers, service names. What remains is the vulnerability descriptor.
Examples:
- "Missing USER directive in Dockerfile" → `Missing USER directive`
- "SQL injection in views.py:142 via user_id" → `SQL injection`
- "Hardcoded password in config/settings.py" → `Hardcoded password`

Group findings with identical or near-identical normalized titles.

**Signal 4 — File pattern + title combination**
When none of the above produce clean groups, use the combination of:
- File type (`Dockerfile`, `*views.py`, `*serializers.py`, `*.yaml`, etc.)
- Vulnerability category from title keywords

**Result:** produce a list of groups, each with:
- `family_name`: short descriptive name for the vulnerability family
- `findings`: list of findings in this group
- `grouping_rationale`: one sentence explaining why these were grouped together

Each finding belongs to exactly one group. If a finding doesn't fit any group,
place it in a catch-all group called "Miscellaneous".

## Step 3 — Read source code for each finding

Source tree root: `/tmp/aist/projects/dev/<project_name>/`

For each finding, construct the absolute path:
```
/tmp/aist/projects/dev/<project_name>/<finding.file_path>
```

Read the file and understand enough context to produce a specific remediation:
- The exact vulnerable construct at `finding.line`
- The enclosing function or block
- Data flow into the vulnerable point
- Any existing mitigations nearby

Read as much of the file as needed — do not apply an artificial line limit.
Start from `finding.line` and expand outward until the context is clear.

If the file cannot be read (does not exist, binary):
fall back to generating remediation from finding metadata only — note this in the row.

## Step 4 — Generate specific remediation per finding

Based on the code read in Step 3, write a one-to-two sentence remediation that:
- Names the exact construct to change (function name, directive, variable)
- States the concrete action (replace X with Y, add Z before W)
- Is copy-paste actionable — a developer who has never seen this code can act on it

**Good:** "In the final Alpine stage, add `RUN addgroup -S app && adduser -S -G app app`,
run `chown -R app:app /opt/service`, then add `USER app` before the existing `CMD`."
**Bad:** "Run the container as a non-root user."

**Findings requiring special handling** — flag instead of giving a generic fix:
- Finding is in vendored or generated code → "Review separately: vendored file"
- Fix requires architectural change → "Review separately: \<reason\>"
- The vulnerability is intentional / documented exception → "Review separately: verify documented exception"

## Step 5 — Compose the Jira description

For each finding family, generate one `.md` file with this structure:

```markdown
## Summary

<one line: "Remediate <vulnerability type> in <project_name>">

## Description

<2-3 sentences: what the problem is, what pattern causes it, why this set of findings
belongs together>

<bulleted general remediation approach applicable to most findings in this family>

## Impact

<2-4 sentences: concrete security or reliability impact. What an attacker can do,
what data or system is at risk>

## Expected Fix

<general fix pattern with representative code snippet(s)>
<show variants if fix differs by OS, language, or framework>
<use <placeholder> for values that vary per finding>
<if some findings need special handling, describe that category separately>

## Affected findings

<one subsection per project path group, sorted alphabetically>

### <project/path/prefix>

| Location | Suggested remediation | SAST URL |
|---|---|---|
| [path/to/File#LNN](<sourcefile_link if available, else plain path>) | <specific fix from Step 4> | [<id>](https://aist.itsec-europe.com/findings/<id>) |
```

**Project path prefix** within each family:
Take all `file_path` values in the subgroup, find their longest common directory prefix.
If that prefix is empty or just `/` (too broad), use the first 2-3 path segments per file
and group by those.

**Multiple findings in the same file:** merge into one table row.
Location column: link to the file (omit line anchor if lines differ across merged findings).
SAST URL column: comma-separated links for all finding IDs.

## Step 6 — Save output

- File name: `jira_<family_slug>_<project_name>.md`
  where `family_slug` = `family_name` lowercased, spaces/special chars → `_`, max 40 chars
- Save to `output_dir` (default: current directory)
- After saving, print the full content of each file to stdout

If multiple families were processed, also print an index:
```
Generated N Jira descriptions for project '<project_name>':
1. jira_docker_root_<project_name>.md — "Remediate Docker images running as root" (12 findings, 4 path groups)
2. jira_sql_injection_<project_name>.md — "Remediate SQL injection" (3 findings, 2 path groups)
```

## Step 7 — Self-check before finalizing

- [ ] Every fetched finding appears in exactly one table row
- [ ] Grouping rationale is documented and `family_name` reflects it clearly
- [ ] Specific remediations are actionable, not generic
- [ ] Findings flagged "Review separately" have a stated reason
- [ ] No finding-specific details leaked into the general Description/Impact sections
- [ ] Source file was actually read for each finding (or fallback noted)
- [ ] AIST finding URLs use correct IDs
- [ ] All output files are `.md`

## How to trigger

```
/aist-jira-description project_name=nx
/aist-jira-description project_name=nx severity=critical
/aist-jira-description project_name=nx severity=high output_dir=docs/jira
```
