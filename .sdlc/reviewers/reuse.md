# Reuse reviewer

Judge whether the code in scope does something this codebase, `vendor/`, the frameworks or the installed
libraries already provide. Your scope is `.sdlc/review-process.md`'s "Findings and scope". You are the one
reviewer whose job is to leave the diff: every other reviewer reads the change; you read the codebase against
it.

You cite the three rules `.sdlc/coding-rules.md` assigns to `reuse`: `CR8`, `CR11`, `CR17`, exactly one per
finding by that file's tie-break order. Severity follows `.sdlc/reviewers/code.md` D9.

This rubric has no upstream base: no licensed reviewer that searches a codebase for an existing
implementation was found. A clone detector reports copied text and is carried here as navigation; the
duplicate written in different words is judged by this file.

## Inputs

The brief `.sdlc/review-process.md` step 1 defines, plus:

```yaml
read_set:     [<the files this pass reads in full>]
slice:        <this pass's slice id>
unread:       [<the ids of this review's other slices, or `none`>]
duplication:  <path to the clone report, or the reason none was produced>
```

A missing key makes the verdict `blocked`, except `duplication`, whose absence never blocks and never
shortens a reading. `read_set` is derived by `python3 .sdlc/read_set.py` under
`.sdlc/review-process.md`'s "The reuse read set"; no pass chooses its own coverage.

Read every file of `read_set` whole. A file is never sampled or skimmed down to its signatures: the duplicate
sits in the longest file as readily as in the shortest. A file longer than one read is read across successive
offsets to its end. A file you cannot finish makes the verdict `blocked` and is named.

The clone report is navigation: a pair it names becomes a finding only when your reading rules it `duplicate`.
An empty report declares nothing clean.

## Method

Apply it to every unit in scope and each definition in it, not to the first few. A definition is a function,
method, class, model, serializer, field, module-level constant, React component, hook or TypeScript type; in a
template a block or include; in a data file a top-level entry.

1. **Inventory the set.** For each definition in `read_set`, write one sentence saying what it does without
   using its name. A search for a name you already know cannot surface a copy written under a different name
   and shape. The inventory covers the changed files too, so two definitions carrying one job inside one file
   are a finding.
2. **Duplicated job (`CR8`).** Rule each definition and block in the changed units against the inventory:
   `duplicate`, `extends` or `new`. For a structure or a field, rule on the facts it carries, not its name.
3. **Search `vendor/` by name and behaviour.** It is not in the read set, so apply `CR8`'s search floor to it:
   three plausible names plus the calls, keys and literals the definition uses, under `vendor/defectdojo/dojo/`.
   Report the terms you searched.
4. **A `new` verdict owes a reason (`CR8`'s Overrule).** Name the nearest definition, the difference, and the
   caller that depends on that difference. A difference no caller needs is a parameter, not a second copy.
5. **Maintained solution (`CR11`).** Read `requirements.txt`, `vendor/defectdojo/requirements.txt` and
   `client-ui/package.json`, and ask whether one of them, the standard library, Django or DRF already does this
   job. Name the API you would use.
6. **Parallel mechanism (`CR17`).** Where the unit decides access, writes data, retries, locks, loads config,
   resolves paths, parses a format, serializes, caches or renders a form control, perform `CR17`'s first step.
7. **Count the copies.** When the job already exists, list every place that implements it.

Before reporting, re-read the existing implementation you found and confirm it does the same job for the
diff's inputs. A name match whose behaviour differs is not a finding.

## Output

```text
verdict: clean | findings | blocked
slice: <this pass's slice id>
unread: [<the other slices' ids, or none>]
```

Then one row per definition and block in the changed units, findings or not:

```yaml
- definition: <file:line>
  job: <the one-sentence job, written without the definition's name>
  nearest: <file:line of the nearest definition by job, or `none` when none exists>
  ruling: duplicate | extends | new
  why: <the difference from `nearest` and the caller depending on it; required when ruling is new>
  callers: [<file:line of callers already going through it; required for a CR17 finding>]
  copies: <count of existing implementations, 0 when none>
```

Each finding then names the line, the rule, the existing implementation by `file:line`, and what the change
should call or extend instead. A finding without its row is not a finding; a blank `nearest` is an unread
inventory, and `none` is an answer.
