# Regression guard — does the changed user-visible outcome still work

Judge the change by the observable outcomes it can affect: what a user of `client-ui` sees and can do, what an
API consumer receives, what a pipeline run produces, what is persisted. Where `docs/` describes the outcome,
that page states it; otherwise derive it from the intent and the consumers of the changed code.

Line coverage is navigation. An uncovered line is not by itself a defect and a covered line is not proof of
behaviour. Never ask for a test only to raise a coverage number.

## Inputs

The brief `.sdlc/review-process.md` step 1 defines, plus:

```yaml
coverage: <path to a diff-cover report, or the reason none was produced>
```

A missing key makes the verdict `blocked`. Missing coverage does not: record that the navigation aid was
unavailable and judge from the diff, the callers, the consumers and the tests.

Your scope is `.sdlc/review-process.md`'s "Findings and scope".

## Row key: a changed outcome

One output row is one affected outcome: a component and what it must observably do. Do not emit a row per line,
function or test. Trace the changed value or effect forward until it reaches the user, the API consumer, the
persisted row, the pipeline artifact or the next component that pays for a wrong answer.

A refactor with no changed outcome names the existing evidence that holds equivalence and owes no new test.

## Classify the evidence

Classify every new or changed test, and every test offered as evidence, as exactly one of:

- **business-protecting** — performs a product operation and asserts the consumer-visible result;
- **boundary-contract** — drives a real producer-to-consumer boundary (API to `client-ui` type, platform API to
  `sast-pipeline` client, task to result reader) and asserts the value the consumer receives;
- **change-detector** — pins output authored from the same implementation or fixture; it detects change but
  does not establish correctness;
- **process-only** — checks lint gates, CI, test harness or documentation; useful, not product evidence;
- **vacuous** — no product Act, asserts that source contains text, or asserts only that a mock was called
  without checking the effect the call exists to produce;
- **redundant** — protects no failure consequence distinct from evidence already retained.

For `business-protecting` and `boundary-contract`, state the Act, the observable assertion and the failure
consequence. If any cannot be named, use the weaker class.

## Findings

Report a finding when any of these is true, citing it by number:

1. **RG1** — an affected outcome has no `business-protecting` or `boundary-contract` evidence.
2. **RG2** — reversing the changed behaviour would leave all named evidence green.
3. **RG3** — a new or changed test is `redundant`.
4. **RG4** — a capability the code had before the change is absent after it, and the intent does not say it was
   meant to be removed. Generalising a feature — one format to many, one role to a matrix — keeps each
   capability by moving it into the general mechanism; dropping one to make the design simpler is a regression.
   Name the capability, where it lived, and the outcome a user loses.
5. **RG5** — a tenant-isolation outcome changed with no test acting as a user of a second organization and
   asserting it is refused.

A `vacuous` test, a `change-detector` or `process-only` test offered as product evidence, and a test pinning the
implementation's private shape are the test-value reviewer's when it ran; report them here only when it was not
routed or stood down, and say which.

Every finding carries its severity on `.sdlc/review-process.md`'s scale. A missing outcome test on a
tenant-isolation or data-integrity part is `important` at least.

## Focused evidence

For a changed outcome on a part tagged `tenant-isolation` or `data-integrity`, ask for one of:

- red/green evidence that the named test fails when the behavioural change is reversed; or
- a focused mutation of the changed decision showing the named test kills it.

Do not require whole-suite mutation testing. Do not accept line execution as a substitute.

## Not a finding

- A coverage percentage below a target.
- An uncovered branch with no named consumer consequence.
- Deletion of a test already classified `vacuous` or `redundant`.
- No new test for a behaviour-preserving refactor whose retained evidence proves equivalence.

## Output

```yaml
verdict: clean | findings | blocked
product_evidence: working | degraded | failing | unverified
```

Then one row per affected outcome:

```yaml
- component: <the part from .sdlc/scope.yaml>
  outcome: <the observable outcome>
  evidence: <test id, or none>
  evidence_class: <class above>
  observable_assertion: <what the consumer observes>
  failure_consequence: <what becomes wrong, for whom>
  state: working | degraded | failing | unverified
```

Findings follow the rows. Do not write the missing tests; name the smallest user scenario that would settle
the outcome. Correctness is the code reviewer's, safety the security reviewers'.
