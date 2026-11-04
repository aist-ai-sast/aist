# Review process

A change is ready to commit when every reviewer `.sdlc/scope.yaml` routes it to has returned a verdict and
every finding has a disposition. This file is the one statement of how that happens. Committing stays the
operator's step.

The author is whoever wrote the change. The dispatcher is whoever runs this process; in a single session
they are the same agent.

## 1. Fix the change and write the brief

Run this once the author believes the change is complete and has run the tests the change owes.

1. Fix the diff in one file, `.sdlc/work/diffs/<id>.diff`, where `<id>` is the first 12 hex digits of the
   file's SHA-256. It holds, in this order:
   - `git diff HEAD` — staged and unstaged changes to tracked files;
   - every untracked file in full, as `git diff --no-index /dev/null <path>` produces it, listed by
     `git ls-files --others --exclude-standard`. A new file absent from the diff is a file nobody reviews;
   - for a changed submodule pointer (`sast-combinator`), `git -C sast-combinator diff <old>..<new>` plus that
     submodule's own working-tree changes, built the same way.
2. Route it: `python3 .sdlc/route.py` prints the changed paths by part, each part's criticality tags, and the
   reviewers the change needs; `--json` prints the same as each reviewer's paths.
3. Write the brief, `.sdlc/work/diffs/<id>.brief.yaml`, with the keys every rubric reads:

   ```yaml
   id:           <the 12-digit id>
   diff:         .sdlc/work/diffs/<id>.diff
   intent:       <one paragraph: what the change is for, in the author's words>
   tests_run:    <the command run and its result, or `none`>
   criticality:  <the tags route.py printed for the changed parts>
   ```

   Each dispatch adds `paths:` — the changed paths `route.py --json` lists for that reviewer. A reviewer judges
   the units of those paths under "Findings and scope"; the rest of the diff is context it reads, not a subject
   it rules on. A rubric that needs more names its extra keys; the dispatcher supplies them beside the brief.

The diff is fixed before the first dispatch and does not change until every reviewer has answered. Any later
edit, including a formatting fix, makes a new diff with a new id.

## 2. Dispatch

Dispatch every routed reviewer in parallel, each with the brief's path. A reviewer's reply is valid only when
it begins with the rubric's `verdict:` line; a transport completion is not a verdict, and a silent reply is a
reviewer that did not run.

| Reviewer in `scope.yaml` | Agent | Rubric |
|---|---|---|
| `api` | `aist-api-reviewer` | the agent file |
| `security` | `aist-security-checker` | the agent file |
| `ui-security` | `aist-ui-security-checker` | the agent file |
| `migration` | `aist-migration-validator` | the agent file |
| `code` | `aist-code-reviewer` | `.sdlc/reviewers/code.md` |
| `reuse` | `aist-reuse-reviewer` | `.sdlc/reviewers/reuse.md` |
| `structure` | `aist-structure-reviewer` | `.sdlc/reviewers/structure.md` |
| `test-value` | `aist-test-value-reviewer` | `.sdlc/reviewers/test-value.md` |
| `comment` | `aist-comment-reviewer` | `.sdlc/reviewers/comment.md` |
| `regression` | `aist-regression-guard` | `.sdlc/reviewers/regression.md` |
| `architecture` | `aist-architecture-reviewer` | `.sdlc/reviewers/architecture.md` |
| `docs` | `aist-architecture-docs-maintainer`, audit mode | `.codex/skills/security-architecture-maintainer/SKILL.md` |

A harness without these agents dispatches a subagent with the rubric as its instructions.

### Reviewers that stand down on a condition

Each condition is read off the diff, not judged:

- `comment` — runs when the diff adds or changes a comment or docstring, or changes any line of a unit that
  carries one;
- `test-value` — runs when the diff adds or changes a test;
- `architecture` — runs when the diff adds a module, adds an import between top-level packages
  (`aist`, `aist_site`, `client-ui`, `sast-combinator`, `vendor`), changes a public signature, or adds a writer
  to a model or file another module owns;
- `docs` — runs when the diff changes a page under `docs/`.

A reviewer that stood down is named in the review record with the condition that was not met. One missing
from both the reviewers run and that list was skipped, not stood down.

### What the dispatcher supplies beyond the brief

- `structure`: `complexity`, the `CR5` measurement over the changed Python files, and `module_size`, each
  changed module's line count before and after. On a change with no Python, each key carries that reason.
- `reuse`: the read set and the clone report, under "The reuse read set" below.
- `regression`: `coverage`, the path to a diff-cover report when one was produced, or the reason none was.

## 3. Findings and scope

This section is the one statement of what a reviewer judges. Rubrics point here and do not restate it.

A reviewer reports:

- **every defect in a changed unit**, whether the diff introduced it or it predates the change;
- **every defect the diff introduces or worsens outside the changed units** — a caller broken by a changed
  signature, a consumer of a renamed key, a page that now states something false — anchored at the changed
  line that causes it and naming the line it affects.

The unit that contains a changed line is:

- in code, the innermost function, method, class or React component around it; a line outside any of them
  belongs to its module; a rule about a module as a whole is judged for the module;
- in a template, the innermost block or included partial;
- in a document, the section under the nearest heading above it;
- in a configuration or data file, the innermost mapping entry or list item around it.

A unit the diff removes is judged for what its removal breaks. Anything else is read only as context. No list
of known defects is ever an input to a review.

Every finding names `file:line`, the rule or check it cites, a severity, and the consequence — what breaks,
for whom — rather than the rule's name repeated.

### Severity

One scale for every reviewer: `critical`, `important`, `minor`. `.sdlc/reviewers/code.md` calibrates it for
the coding rules; each security rubric calibrates it for its own checks. A rationale never lowers a severity:
"deliberate", "simplest thing that works", "consistent with the surrounding code" belong in a disposition,
where someone independent weighs them.

## 4. Dispositions

Every finding, whatever its severity, ends with exactly one disposition:

- `fixed` — the change now addresses it, and the scoped re-review says so;
- `rejected` — the finding is wrong: the code was misread, the rule does not apply, the defect cannot occur, or
  its anchor is outside what "Findings and scope" makes reportable. The author writes the ruling, and
  `aist-adjudicator` judges it;
- `escalated` — the finding is real and fixing it collides with a named rule, or needs a decision only the
  operator can make. The ruling names the decision and the adjudicator confirms it.

There is no deferral. "Follow-up", "known debt" and "out of scope" for a reportable defect are not
dispositions; the adjudicator overturns them, and the finding is fixed in this change.

An `overturned` adjudication returns the finding to the author as open. An `escalate` adjudication, or any
finding still open after the last round, makes the change `blocked`: it is not ready to commit, and the
operator receives the open findings with their rulings.

## 5. Merging findings that share an anchor

When two or more findings that each cite a rule of `.sdlc/coding-rules.md` share a `file:line`, the dispatcher
merges them into one:

- the merged finding cites the rule that comes first in that file's tie-break order; a rule absent from the
  order ranks after every rule in it, and among absent rules the lower number ranks first;
- it carries the highest severity of the findings merged into it;
- the other findings stay in it as evidence, each with its reviewer and rule.

A finding citing a rubric's own check — a security, API, UI, migration, architecture or regression check — is
never merged, and never folded into one that cites a coding rule.

## 6. Fix loop

Every finding goes back verbatim to the author, who fixes it. The scoped re-review receives the original
findings and the fix diff; for each finding it returns `addressed` or `not addressed`, and it judges the units
the fix diff changes under "Findings and scope", as the first reading judged the change.

A fix round is the author's batch of fixes plus its re-review. Three rounds is the cap. Every round records
which findings were addressed, which remain, and what the fix changed. A finding open after the third round is
escalated; it is never closed by a ruling.

Open findings are not sent to the adjudicator to end a round early.

## 7. Independent reading

A reviewer does not review its own rubric's change. A change to a rubric, an agent file or this process goes to
the operator for a human-readable diff review.

A `clean` verdict from `api` or `security` on a change of more than one file receives a blind second reading by
the same reviewer, dispatched without the first report. If the readings disagree, the union is the finding set
and the normal fix loop runs.

## 8. The reuse read set

This section is the one statement of what `reuse` reads; `.sdlc/reviewers/reuse.md` names it and states what a
pass does with it. `python3 .sdlc/read_set.py` derives it, so no pass chooses its own coverage.

1. **Membership.** Every file `git grep -I` lists — git searches only tracked files and classifies text as
   `.gitattributes` directs — that `scope.yaml` assigns to a part whose reviewers include `reuse`. Built from
   git's index, never by walking directories, so untracked material never enters a read set. A source file that carries a
   NUL byte on purpose is marked `diff` in `.gitattributes`, or git counts it binary and no pass reads it.
2. **Out.** The paths a part's `reuse_excludes` lists, and what no reuse-routed part claims. `vendor/` is out
   because the part that claims it does not route to `reuse`; a vendored path a reuse part ever claims goes in
   that part's `reuse_excludes`. `vendor/` is still part of the codebase for `CR8`; a pass searches it by name
   and behaviour, the floor `CR8` states, rather than reading it whole.
3. **Coverage is whole.** Every review reads the whole set. A duplicate of what a change adds sits as often in a
   part the change does not touch as in one it does.
4. **Slices.** The set is cut into slices of at most 409,600 bytes, in sorted path order. A file is never split,
   and a file larger than that is a slice of its own. A slice's id is the lowest-sorting path it contains.
5. **The leading slices** carry the read-set files that share a directory with a changed file, cut by the same
   size; the rest of the set follows in slices of its own.
6. **Every pass carries the changed files, the diff and the clone report** beside its own slice, so each pass
   rules every definition the change adds against what it read.
7. **Dispatch.** Every slice is dispatched as its own pass, told the other ids as `unread`. A review with a slice
   that was never dispatched, or that returned nothing, is incomplete, not clean.
8. **Merging the passes.** One definition collects one ruling per pass; the merged ruling is the strongest any
   pass returned — `duplicate` over `extends` over `new` — with the slice id and `nearest` that produced it.
9. **The clone report** is
   `pylint --exit-zero --disable=all --enable=duplicate-code --min-similarity-lines=4 <package>` over each
   Python package the diff touches, cut to entries that name a changed file. It is navigation and rules nothing.
   On a change with no Python the key carries that reason.

## 9. The review record

The dispatcher writes `.sdlc/work/reviews/<id>.md` and reports the same content to the operator. Machine-read
state stays in front matter; nothing reads the body as state.

```markdown
---
id: <the diff id>
covers:
  - <path>
reviewers_run: [<name>]
stood_down: [<name: the unmet condition>]
verdict: clean | findings | blocked
---

## Findings

### <title>

- reviewer: <name>
- cites: <rule or check id>
- severity: critical | important | minor
- anchor: <file:line>
- disposition: fixed | rejected | escalated
- ruling: <required unless fixed>
- adjudication: <required for every ruling: upheld | overturned | escalate, with the reason>

## Rounds

- <round: addressed and open findings, and what the fix changed>

## Checks run

- <command and result>

## Reuse slices

- <slice id and the verdict its pass returned; present only when reuse ran>
```

`clean` means every required reading was clean. `findings` means every finding is fixed, or rejected with an
upheld adjudication. `blocked` means a finding is escalated or still open; the record names the decision the
operator owes. `.sdlc/work/` is git-ignored: the record covers one uncommitted diff and stops meaning anything
once that diff is committed.
