# Coding rules

Rules for judging one change to code. Each states a **test to apply to a diff**, not a principle.

**Two audiences, marked per rule.** Most rules are read at *review* time, against a diff. A few carry
a **tripwire** — a check applied at *write* time, on the text being produced. A tripwire fires on
literal tokens and carries no judgement; what the firing obliges is applying the rule's test, not
reporting a finding.

**Severity is defined once**, in `.sdlc/reviewers/code.md`. Rules here say only whether they are
normally important or normally minor, and why.

**Where a rule has a name in the literature, the name is given.** A named smell means the same thing
to any reviewer, human or machine.

**Scope is not stated here.** Every test below is applied to what `.sdlc/review-process.md`'s "Findings
and scope" makes reportable. Where a test says "the change" or "this diff", it means that scope.

**Counts are not written here.** A number in prose goes stale and becomes the defect `CR9` warns about.
Where a rule refers to the state of the codebase, it gives the command that measures it.

## What a committed check already decides

A reviewer does not report what one of these reports. It reads them to know what they cover, and cites
their result as evidence where it bears on a finding.

- `ruff check` under `ruff.toml` — among others, imports outside module scope, `print`, bare `except`
  with `pass`, implicit namespace packages, the repository complexity ceiling.
- `aist/test/test_authz_lint.py` — in `aist/api/`: direct `request.data` handling, `get_object_or_404`
  and `.objects.get` on an organization-owned model, `Permissions.*`, `authz = PUBLIC` outside its
  allowlist.
- `aist/test/test_authz_conformance.py` — every routed AIST view is an `AISTAPIView` with a valid `authz`.
- `aist/test/test_dast_source_requirement_lint.py` — the source-repository requirement is read only
  through its accessor.

A finding that is decidable without judgement belongs in one of these rather than in a review forever.
Say so when you see one: "this is checkable — `<the property>`".

## Which reviewer owns which rule

Each rule has exactly one owning reviewer, and a reviewer cites only the rules it owns, so one defect
produces one finding.

- `code` (`.sdlc/reviewers/code.md`) — CR1, CR3, CR4, CR7, CR10, CR12, CR13, CR14, CR15, CR16, CR20, CR21.
- `reuse` (`.sdlc/reviewers/reuse.md`) — CR8, CR11, CR17.
- `structure` (`.sdlc/reviewers/structure.md`) — CR2, CR5, CR18, CR19.
- `test-value` (`.sdlc/reviewers/test-value.md`) — CR6.
- `comment` (`.sdlc/reviewers/comment.md`) — CR9.

`aist-security-checker`, `aist-api-reviewer`, `aist-ui-security-checker`, `aist-migration-validator`,
`architecture` and `regression` judge by their own numbered checks and cite those, not a rule here.

When the owner is not routed for the changed path, or stood down under `.sdlc/review-process.md`, the
reviewer that sees the defect reports it under the owner's rule and says which of the two applied. An
unrouted owner is a `.sdlc/scope.yaml` gap, not a reason to stay silent.

## Which rule to cite when several could apply

Cite exactly one, by this order — first match wins — so two reviewers reach the same citation.

1. The code does not do what it says, or cannot run → **CR15**.
2. A background task cannot run twice, or after its object is gone → **CR21**.
3. The value crosses a component boundary and is not in a structured field → **CR14**.
4. The input has a grammar and is read by pattern-matching, **or a bulk rename was applied by
   substitution** → **CR1**.
5. Something maintained already does this → **CR11**.
6. The code runs a second mechanism beside one this codebase already runs → **CR17**.
7. Something in *this* codebase, `vendor/` included, already does this or holds this data → **CR8**.
8. The behaviour is spread across units, or one unit changes for unrelated reasons → **CR2**.
9. One unit is too large to read in a pass → **CR5**.
10. A structured value travels as a raw dict → **CR18**.
11. A closed set of values travels as bare strings → **CR19**.
12. A literal encodes a policy, threshold, environment, host, account, path or limit → **CR4**.
13. User-facing text names the base platform → **CR20**.
14. A comment states something the code beside it does not do → **CR9**.
15. A tool or endpoint an agent reads returns the artifact where the caller asked about one thing → **CR16**.

`CR15` is about code that misbehaves against its own name or contract. Correct code under a comment that
misdescribes it is `CR9`; the code is not the defect.

`CR1` is not a special case of `CR11`; a regular expression over YAML is `CR1` even though a parser exists.

---

## CR15 — The code does not do what it says, or cannot run

*review time · important, and critical when it cannot complete or inverts a signal*

**Test.** Trace the change's own execution against the artifacts and interfaces it names. For every name
it depends on, confirm the name exists and holds what the code assumes:

- an attribute, field or key it reads — does any writer produce it, under that spelling?
- a file it opens — does anything write that path?
- a function or endpoint it calls — does the signature match, and can the return be what it assumes?
- a collection or queryset it operates on — is it the one it means, or one that merely resembles it?
- a comparison against a literal — does the value it compares to ever take that form?

**Instead.** Name the writer, the producer, the caller. If nothing produces the name, that is the finding.

**Severity.** Important as a floor. Critical when the code cannot complete its stated purpose, or when it
inverts a user-facing signal — showing "no findings" where the truth is "could not read the findings" is
worse than failing.

**Overrule.** "It is obviously a typo." A name no writer produces is a dependency on something that does
not exist, and it survives review precisely because it looks trivial.

## CR21 — A background task that cannot run twice, or after its object is gone

*review time · normally important*

**Test.** For a Celery task the change adds or alters:

- run twice with the same arguments, does it leave the same state as run once? Redelivery after a worker
  restart, `acks_late`, a retry or a duplicate enqueue all produce the second run;
- when the object its id names no longer exists — a deleted pipeline, project or finding — does it stop
  with a recorded reason, rather than raising into a retry loop or acting on `None`?
- does a state transition it performs check the state it starts from, so a duplicate run is a no-op
  rather than a second transition?

**Instead.** Look the object up so absence is an answer (`.filter(...).first()`), and exit with a log
line naming it. Guard a transition on its source state in the same write (`.filter(status=...).update(...)`)
or under `select_for_update()` inside the transaction.

**Overrule.** "The task is only enqueued once." Enqueue count does not bound execution count.

## CR14 — A machine-consumed value that is not in a structured field

*review time · normally important*

**Test.** Is a value produced by one component and read by another — AIST and `sast-pipeline`, a task and
its result reader, the AI triage bridge and its consumer, the API and `client-ui`? Then it has a fixed key in
a structured field — a typed attribute, a model field, a serializer field, a declared key of a JSON
contract — not a sentence or a string that must be pattern-matched.

**Instead.** Declare the consumed keys. Free-form goes in a clearly marked extra field, never in the
consumed keys.

## CR1 — Pattern-matching input that has a grammar

*review time and write time · normally important*

**Write-time trigger: a bulk rename or renumber.** The throwaway script that performs it is not
committed, so a reviewer sees only the result. The author applies the edit clause below; the reviewer
applies the meaning-preserving assertion to the result.

**Test.** Does the input have a defined format — YAML, JSON, TOML, front matter, HTML, XML, a URL, a path,
a version string, SARIF, the output of a tool that also offers structured output? If a regular expression,
string slicing or `split` reads it, that is the finding.

**Instead.** The format's own parser. Where a tool already computes the answer — `git` for paths and
revisions, `urllib.parse` for URLs, `packaging.version` for versions — ask the tool.

**Why.** Over a structured format the pattern breaks on a valid form it did not anticipate, and the
breakage looks like no matches rather than an error. Over prose it matches text that merely mentions what
it seeks, so the check reads as working and does nothing.

**The same rule covers a pattern used to edit.** A renumbering substitution cascades: a replacement
produces text a later pass matches again, and nothing errors. Derive the mapping from structure, apply each
substitution once through a single pass keyed on that map, and check each citation against the map rather
than checking that it resolves to something.

**Overrule.** "It is only a small pattern." "The format is simple here." The pattern is small until the
input takes an unexpected form.

## CR11 — Hand-written where a maintained solution exists

*review time · normally important*

**Test.** Is there a maintained library, a facility of the standard library, of Django or DRF, or a
component already in `client-ui/package.json`, that already does what this code does? That is the whole
test. NIST SSDF practice PW.4: *reuse existing, well-secured software when feasible instead of
duplicating functionality.*

**Where to look, not what to limit the rule to.** Parsing, matching, scheduling, retry and backoff,
diffing, templating, date and timezone arithmetic, cryptography, protocol clients, path manipulation,
configuration merging, concurrency primitives, form and filter widgets, keyboard and focus handling. A
reimplementation outside every category on this list is still a finding.

**Instead.** The library. If it is genuinely unsuitable, the change says why, specifically.

**Overrule.** "A dependency is a cost." So is a subtly wrong reimplementation, and it is paid silently
and forever.

## CR17 — A second mechanism beside one this codebase already runs

*review time · normally important*

**Test.** Does the code run its own way of doing something the codebase already has an established path
for? The usual shapes here:

- an access decision derived in a view, serializer, task or template beside `aist/authz/` and the getters
  registered for it;
- a lock or uniqueness guard beside the `select_for_update()` pattern the membership service uses;
- a format-specific registry beside `aist/parser_overrides.py`;
- an HTTP session beside the `DefectDojoClient` session in `sast-pipeline`;
- a filter or serializer beside the one `vendor/` already exposes;
- a widget beside the Radix primitives `client-ui` already uses;
- a second reader of one input: two places that read or parse the same source data for two purposes.

The owner is established **from the code**: the module that writes this data, the function every other
caller goes through, the base class the siblings extend. Documentation is not evidence of an owner.

**Required first step.** Name the existing mechanism with its `file:line` and at least two callers that
already go through it. Without them the finding is an impression.

**Instead.** Go through the existing mechanism, or extend it. Where the object that already reads an input
can return the extra fact as part of its result, it does. If the mechanism cannot serve the new case, the
change says why and changes the mechanism rather than growing a parallel one.

**Why distinct from `CR8`.** `CR8` is a duplicated job; this is a duplicated route. A parallel route splits
every future fix in two, and the copy that is not fixed keeps working just well enough to hide.

## CR8 — Reimplementing something this codebase already has

*review time · normally important*

**Test.** Does the code contain a function, class, structure, field, constant, component or block whose
job already exists elsewhere in this codebase (**Duplicated Code**, including its semantic form)? `vendor/`
is part of this codebase for this rule. Identical text is the rare case; the usual case does the same job
under a different name and shape:

- a function that computes what an existing one computes — same inputs, same answer;
- a structure (dataclass, model, serializer, TypedDict, TypeScript type) that carries the same facts as an
  existing one, or a subset of them;
- a field that stores a fact another field of the same entity, or of an entity it references, already
  stores — two places to write one truth;
- a helper whose equivalent is defined in several other modules already, which the change makes one more.

**Required first step.** For each definition or block, write one sentence saying what it does without
using its name. Then search for the existing implementation by **at least three plausible names and by its
behaviour** — the calls it makes, the keys it reads, the literals it compares against — and report what
you searched for. The `reuse` reviewer performs this step by the reading `.sdlc/reviewers/reuse.md`
defines, and reports against that reading's inventory rather than a search log.

**Instead.** Call, extend or reuse the existing thing. Where several copies exist, the change uses one and
replaces the others in this change. A `vendor/` definition is reused by importing, subclassing or wrapping
it from `aist/`; `vendor/` itself is never edited.

**Overrule.** "Mine is slightly different." Name the difference and whether any caller depends on it; a
difference no caller needs is a parameter, not a reason for a second copy.

## CR2 — Behaviour spread across units, or one unit changing for unrelated reasons

*review time · normally important*

**Test.** Would the next change of this kind have to edit several modules (**Shotgun Surgery**)? Does this
module change for two unrelated reasons (**Divergent Change**)? Does the code work mostly on another
object's data rather than its own (**Feature Envy**)? Fowler's catalogue; SRP and GRASP's High Cohesion
underneath.

Two shapes this codebase sees most:

- one rule — an access grant, a derivation, a status decision — written out in a view, a serializer and a
  query, instead of once in the service or model that owns it, with the view calling it;
- two invariants that are independent in meaning merged into one model or flag to save a table. Keeping
  them in two small units is the cohesive design; merging them is the defect.

**When `CR5` also applies**, cite `CR2` and use the size as evidence.

**Instead.** Move the behaviour to the data it works on, or split the unit along the two reasons.

**Overrule.** "It is only a few lines in each place." The number of places is the defect.

## CR5 — A unit too large to read in one pass

*review time · normally important*

**Test.** **Long Method** / **Large Class**: branch count, statement count, cyclomatic complexity of the
unit as it stands after the change. If the behaviour is also spread, cite `CR2` instead.

**Measure with.**
`ruff check --select C901,PLR0911,PLR0912,PLR0913,PLR0915 --config 'lint.mccabe.max-complexity=10' <paths>`.
The repository-wide ceiling in `ruff.toml` exists so existing code passes CI; it is not the bar for a
changed unit.

## CR18 — A structured value carried as a raw dict

*review time · normally important*

**Test.** Is a dict with known keys created, passed through more than one function, stored, or read by key
in a place other than where it was built? Then it is a structure: a `dataclass` (frozen, with slots, where
the value does not change), or a serializer or pydantic model where it is validated at a boundary. The signs
are `.get("key")` spread over several functions, `dict[str, Any]` in a signature, and a docstring that
lists the keys.

**Not this rule.** A one-shot JSON read or write at the edge — a request body handed to its serializer, the
final response envelope — where the dict lives in one function. A value that crosses a component boundary
with no structured field at all is `CR14`.

**Instead.** Declare the type once and construct it where the data enters. A typo is then an error at
construction, not a silent `None` three calls later.

## CR19 — A closed set of values carried as bare strings

*review time · normally important*

**Test.** Is a string literal used as one member of a closed set — compared with `==` or `in`, used as a
dispatch key, stored as a status, kind or mode, or passed as a `choices` value? If the same literal appears
in more than one place, or the set has more than one member, that is the finding (**Primitive Obsession**).

**Instead.** A `TextChoices`, `Enum` or `StrEnum` defined once and used at every comparison, with the string
form kept only at the serialization edge; in TypeScript, a union type or `as const` object exported once.
`typing.Literal` is acceptable when the set is local to one signature.

**Overrule.** "It is only two values." Two values compared in three places are three places to misspell
one of them, and nothing fails when they are.

## CR4 — Values hardcoded where they will need to differ

*review time · important if it would differ per organization, deployment or environment; minor for a
genuine constant of the algorithm*

**Test.** A literal encoding a policy, a threshold, an environment, a host, an account, a path or a limit
(**Magic Number / Magic String**, **Primitive Obsession**).

**Instead.** Deployment facts in settings read from the environment; organization facts on the
organization or project profile; algorithmic constants named in place.

**Overrule.** "It is the same everywhere today." The test is whether it would change for another
organization or deployment, not whether it has yet.

## CR20 — User-facing text names the base platform

*review time and write time · normally important*

**Test.** Does a string a user reads name the platform AIST is built on? User-facing means: text and
attributes rendered by `client-ui`, an email template, an API error `detail` or validation message, a
notification, an OpenAPI summary shown in the UI.

**Tripwire, write time.** The tokens `DefectDojo`, `Defect Dojo` and `dojo` inside a user-facing string.

**Instead.** "AIST", or the function's own name ("admin API", "findings").

**Not this rule.** Code identifiers, import paths, comments, logs, and anything under `vendor/`.

## CR9 — A comment that narrates history instead of stating the contract

*review time and write time · normally important*

**Test.** Does the comment say what the entity does, guarantees and requires — plus the non-obvious reason
it must be that way — or does it narrate what happened to it?

**Test, at the same time: is it true?** Read every claim in the comment against the code it sits on — the
parameters, return, raised errors, side effects and callers it names. A comment that states a contract the
code does not keep is worse than none. A changed function whose unchanged comment no longer holds is a
finding of this change.

**The boundary.** A rationale in the present tense stays true while the design does; keep it only when the
code would otherwise read as a mistake. A narrative about the past — what was tried first, what a reviewer
asked, what the earlier version did — is stale at the next change and belongs in the commit message.

**Tripwire, write time.** These tokens oblige the boundary test, not a verdict: `earlier`, `previously`,
`used to`, `no longer`, `was changed`, `originally`, `at first`, `turned out`, `now`, a date, a ticket or
version number, or the name of a person or agent.

**On length.** One line where one line does. A comment longer than the code it describes, or one that walks
through the implementation step by step, fails this. Longer rationale goes in `docs/`.

**Exception the repository already follows.** A tunable in settings carries a comment saying how its value
was sized, at the length of its neighbours.

## CR16 — Output an agent reads, sized by the artifact rather than the question

*review time · normally important*

**Test.** Does a tool or endpoint whose consumer is an agent — an MCP tool of `context_extractor_service`,
an endpoint the AI triage bridge calls — return more than the answer to the question asked: a whole file
where one symbol was named, every row where one was asked for?

**Why.** Everything that enters an agent's context is re-read by every later call it makes, so output is
paid once per remaining turn, and a truncated context drops the part the agent needed.

**Instead.** Return the answer plus a handle to the detail, and add the narrower query where the agent has
none.

**Not this rule.** Output a person reads: the UI, a report, a terminal.

## CR3 — Reaching past an interface

*review time · normally important*

**Test.** Does the code read or write another object's private members, depend on a private name in
`vendor/`, or chain through more than one dot to reach data (**Inappropriate Intimacy**, **Message
Chains**, Law of Demeter)?

**Overrule.** "The surrounding code already does it." Then the surrounding code in the unit is fixed too.

## CR6 — A test that would pass regardless of behaviour

*review time · normally important*

**Test.** Does each new or changed test perform a real action on the system under test and assert on its
observable result — a response, a persisted row, a rendered control, a task's effect? Test the port, not the
internals — Beck's and Cooper's formulation.

**Four test shapes are not written at all**, whatever they assert:

- a test that reads a source, skill, doc or config file and asserts on its text, keys or structure — it pins
  the file's wording, fails on every legitimate edit, and passes while the behaviour the file drives is
  wrong. A config file's effect is tested by loading it through the product's loader and asserting what the
  product then does;
- a test whose subject is the test suite's own machinery — its helpers, fixtures, factories or fakes;
- a test that loads data recorded from a real run — it pins whatever the product produced that day, right
  or wrong. Inputs are built by the product's own constructors or factories from values the test states;
- a test or fixture carrying production values — a real hostname, organization or tenant name, branch name,
  commit SHA, run or pipeline id, or a string copied from a production report or log. Keep the shape
  (length, format, field combination) and invent the content.

Committed lint gates (`aist/test/test_*_lint.py`) are checks, not product tests. They are judged by whether
they would fail on the shape they forbid, and they are not offered as evidence that a product outcome works.

## CR7 — No test or check would fail if this change were reverted

*review time · important for behaviour; minor for refactoring where existing tests already pin it*

**Test.** Name the user-visible outcome this change introduces or alters, then name the test that performs
its operation and asserts the result. If no such evidence exists, that is the finding. A changed line,
branch or private detail creates no test obligation without a changed observable outcome.

**Instead.** One outcome-level test that fails when the behavioural change is reverted, written as a real
user scenario. A behaviour-preserving refactor relies on retained evidence and does not owe a new test.

**Every case the change claims, not one representative.** Where a change claims a set — every endpoint,
every role, every format — reverting one member is not the check. One case per claimed member.

**Overrule.** "It is covered indirectly." Then name the indirect test and the assertion that would fail.

## CR10 — A local fix that worsens coupling or breaks another caller

*review time · normally important*

**Test.** What else consumes what this change touched — `client-ui` through the API, `sast-pipeline`
through its client, the MCP server through the platform API, a template through its context — and does the
change hold for those callers? Judge the coupling by **connascence**: its strength (name, type, meaning,
position, algorithm, execution, timing, value, identity, in rising order), how many places share it, and
how far apart they are. A change should reduce strength or bring the coupled places closer, not the
reverse.

**Overrule.** "It fixes the reported problem." That is the minimum. "It can be generalised later." Name what
would have to change, and whether anything would then depend on the narrow form.

## CR12 — A failure path that produces no signal

*review time · normally important*

**Test.** Does the change swallow an exception, catch broadly without handling, continue past an error, or
return a success-shaped value on failure? An error path is a defect when its outcome is indistinguishable
from success — to the user, to the task's result reader, or to the person reading the logs.

**Instead.** Handle it, or re-raise. Where a component must keep running, it records the failure where a
person or a check will see it.

**Measure existing instances with.** `ruff check --select BLE,S110,S112 <paths>`

## CR13 — The change touches a sensitive seam, so it is not yours to judge

*review time · routing rule, not a security rule*

**Test.** Does the change touch any of these?

- authentication backends, API tokens, sessions, membership and role resolution;
- the lookup of an organization-owned object, or a queryset that feeds a response;
- integration credentials, SCM tokens, VPN configuration — their storage, serializer fields, logs and task
  results;
- a file path read or served from a project checkout, including in `context_extractor_service`;
- a subprocess, a container, its mounts or its networking;
- an outbound request: SCM, webhooks, VPN, the AI bridge;
- what is written into an API response, an email, a log line or a task result.

**What to do.** Name the seam, state which lines touch it, and record which review is required:
`aist-api-reviewer` for tenant scoping and object lookup, `aist-security-checker` for the rest. **Do not
assess exploitability or assign a CWE** — that is those rubrics' calibration.

**Why this is here at all.** Recognising that a diff reaches a sensitive seam is mechanical and belongs with
whoever reads the diff first. Judging what it means is not.
