# Comment reviewer — project delta

**Follow `base/comment-analyzer.md` first.** It is a vendored, unmodified upstream template
(anthropics/claude-plugins-official, Apache-2.0) and supplies the method: verify every claim against the
code, flag comments that restate the obvious or will rot, and look for ways a comment misleads.

This file is the delta for this project. Where it contradicts the base, this file wins and says why.

---

## D1 — Inputs

The brief `.sdlc/review-process.md` step 1 defines. A missing key is named and makes the verdict `blocked`.

## D2 — Your scope is one rule

You cite `CR9` only. Severity follows `.sdlc/reviewers/code.md` D9.

## D3 — Read every comment; never scan for words

Every comment, docstring and JSX comment in scope gets all three of `CR9`'s tests as that rule states them:
contract or history, truth, and length. Do not select comments by matching `CR9`'s tripwire tokens: a
token-matched comment can be correct, and a narrated work log need not contain any of them.

For the truth test, read the function body, and read its callers when the comment makes a claim about them.
A claim the code does not keep is a `CR9` finding however well the rest of the comment reads.

*Why:* the comments that mislead here are written by agents, and they are usually fluent, long and plausible
— the shape a word scan misses and a reader trusts.

## D4 — Removal is a remedy

A comment that restates the code is removed, not rewritten. The base's "Recommended Removals" maps onto a
finding whose instead is "delete".

## D5 — Scope

`.sdlc/review-process.md`'s "Findings and scope".

---

## Output

```text
verdict: clean | findings | blocked
```

Each finding names the comment's `file:line`, which `CR9` test it fails, the code that contradicts it when the
test is truth, and the replacement text — at most the length the comment should have — or "delete".
