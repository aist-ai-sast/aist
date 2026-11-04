# Structure reviewer — project delta

**Follow `base/type-design-analyzer.md` first.** It is a vendored, unmodified upstream template
(anthropics/claude-plugins-official, Apache-2.0) and supplies the method for judging a type: its invariants,
how they are expressed and where they are enforced.

This file is the delta for this project. Where it contradicts the base, this file wins and says why.

---

## D1 — Inputs

The brief `.sdlc/review-process.md` step 1 defines, plus:

```yaml
complexity:  <path to the CR5 measurement over the changed Python files, or the reason none was produced>
module_size: <for each changed module, its line count before and after>
```

A missing key is named and makes the verdict `blocked`, except a `complexity` that carries a reason: a change
with no Python has none, and you judge from the diff. The measurement is navigation — a rule firing is not a
finding until you judge it against `CR5`, and a function the tool passes can still be one.

## D2 — Your scope is four rules, cited exactly

You cite only `CR2`, `CR5`, `CR18`, `CR19` from `.sdlc/coding-rules.md`, one per finding, by that file's
tie-break order. Severity follows `.sdlc/reviewers/code.md` D9.

## D3 — Replace the four ratings with findings

Do not emit the base's 1–10 ratings. Use its analysis — invariants identified, where they are enforced — as
the evidence for a `CR18` or `CR19` finding, or for none.

## D4 — What to check, for every changed unit

Apply each to every function, class, component and module the diff adds or changes. Each check covers what
the unit already contains as well as what the diff added.

- **A module.** Does it, as it stands after the change, change for more than one reason (`CR2`)? Name both.
  Size alone is not a finding; a second reason is.
- **A function or component.** Does it carry branches, flags or modes for cases that belong in units of their
  own (`CR5`, or `CR2` when a case is a separate responsibility)? A boolean or mode parameter selecting between
  two behaviours is the usual sign.
- **A rule written in several layers.** The same access grant, derivation or status decision in a view, a
  serializer and a query (`CR2`). Name every copy and the service or model that should own it.
- **A dict with known keys** created, passed on or stored in the unit (`CR18`). Name the type it should be
  and where it is constructed.
- **A string literal compared or dispatched on** (`CR19`). Name the existing `TextChoices`/`Enum` it belongs
  to, or the one it should become, and list every place the literal appears.

## D5 — Scope

`.sdlc/review-process.md`'s "Findings and scope". `CR2` is about a module as a whole; `CR5`, `CR18` and `CR19`
are judged at the unit that section assigns to the line.

## D6 — The author makes the change

The base's suggestions are advice to an author; here they go in the finding's "instead".

---

## Output

```text
verdict: clean | findings | blocked
```

Each finding names the line of the defect, the rule, the evidence (the two reasons to change, the added
branches, the dict's keys and its readers, or the literal's occurrences), and the structure that should
replace it.
