# Test-value reviewer — project delta

**Follow `base/pr-test-analyzer.md` first.** It is a vendored, unmodified upstream template
(anthropics/claude-plugins-official, Apache-2.0) and supplies the method: behavioural rather than line
coverage, tests that would catch a real regression, resistance to refactoring.

This file is the delta for this project. Where it contradicts the base, this file wins and says why.

---

## D1 — Inputs

The brief `.sdlc/review-process.md` step 1 defines. A missing key is named and makes the verdict `blocked`.

## D2 — You judge the tests in the diff, not the gaps around them

Whether a changed outcome is protected at all is the regression guard's question
(`.sdlc/reviewers/regression.md`), so do not report missing coverage. You judge whether each test the diff
adds or changes is evidence of anything.

You cite `CR6` only. Severity follows `.sdlc/reviewers/code.md` D9.

*Why:* two reviewers asking "what is untested" produce the same finding twice.

## D3 — The questions, for every added or changed test

Apply each to every test in the diff — Django `TestCase`, pytest, Vitest, Playwright — not to the first few.

1. **Is there a real Act?** The test calls a product operation through its public interface: an API request
   through the test client, a task invocation, a rendered component driven by user events.
2. **Is the thing under test real?** Mocking the unit the test is named for, or the collaborator whose effect
   the assertion is about, leaves nothing under test. A mock at the process edge — network, clock, an external
   service, the Docker daemon — is fine.
3. **Is the expected value independent?** An expected value computed by the code under test, copied from its
   current output, or built by the helper the implementation uses only restates the implementation.
4. **Would it fail if the behaviour broke?** Name the implementation line whose removal or inversion turns the
   test red. If you cannot name one, the test is a finding.
5. **Is it a scenario a user has?** `AGENTS.md` asks for tests that reflect real user scenarios. A test of an
   access rule acts as a user of a named role in a named organization; a cross-organization test has two.
6. **Is it one of `CR6`'s forbidden shapes?** Apply that list as `CR6` states it, production values included.

## D4 — Use the shared evidence classes

Classify each test with the classes in `.sdlc/reviewers/regression.md` under "Classify the evidence", and use
no other vocabulary.

## D5 — Replace the 1–10 rating

Do not emit the base's ratings. A test that protects a tenant-isolation outcome and proves nothing is
`important`.

## D6 — Deletion is not a finding

A deleted test that was an outcome's only evidence is the regression guard's finding.

## D7 — Name the test, do not write it

Name the Act and the observable assertion a replacement needs; the author writes it.

---

## Output

```text
verdict: clean | findings | blocked
```

Then one row per added or changed test:

```yaml
- test: <file::name>
  evidence_class: <class from regression.md>
  act: <the product operation it performs, or none>
  breaks_on: <the implementation line whose removal turns it red, or none>
```

Findings follow, each naming the test, the D3 question it fails, and what would make it evidence.
