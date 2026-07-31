---
name: security-architecture-maintainer
description: Audit or update AIST reader-facing product, architecture, integration, data-flow, operations, and security documentation. Use when documentation quality must be reviewed or when a verified change affects a workflow, boundary, runtime service, deployment, integration, or security control.
---

# AIST Documentation Maintainer

Maintain documentation as a coherent reader experience, not as a record of the
changes that produced it. Work in either audit mode or update mode.

## Choose the mode

- **Audit mode:** inspect the complete reader-documentation set and its
  navigation. Classify pages as keep, revise, rewrite, merge, or retire. Support
  each finding with the reader question the page fails to answer. Do not edit
  unless the user requested fixes.
- **Update mode:** change only the coherent page or page-and-diagram group that
  owns the affected knowledge. Do not batch unrelated pages merely because they
  appeared in the same diff. This also covers authoring the first version of a
  page or diagram for a feature that has no existing page — treat that as
  creating the coherent group, not as a lighter pass. There is no prior version
  to fall back on for judging whether the result is adequate, so the page
  contract and the new-diagram completeness check below apply in full.

Before writing, define a small page contract:

- page type: explanation, how-to, reference, tutorial, or ADR;
- intended reader and the single primary question the page answers;
- what belongs on the page and what belongs elsewhere;
- canonical owner of any repeated concept;
- the job of a visual, if one is needed.

If these answers are unclear, investigate before adding prose.

## Establish the documentation impact

1. Read `AGENTS.md`, `README.md`, `docs/README.md`, and the affected canonical
   pages before editing.
2. In update mode, inspect the diff and verify changed behavior in code,
   configuration, and tests. Treat that evidence as input, not as content to
   paste into the documentation. Do not infer behavior from commit messages or
   task plans.
3. Classify the change by reader need:
   - product concepts and user-visible workflows;
   - component responsibilities and service boundaries;
   - runtime or data flow;
   - operations and troubleshooting;
   - access control, trust boundaries, or security assumptions;
   - implementation-only change with no reader-facing documentation impact.
4. If no durable reader knowledge changed, do not edit reader documentation.
   State why no documentation update is required.

## Choose the canonical destination

Update the smallest set of pages that owns the changed knowledge:

- `docs/product/`: what users can do and how product states behave;
- `docs/architecture/`: stable responsibilities, boundaries, and interactions;
- `docs/data-flows/`: end-to-end movement of data or control;
- `docs/integrations/`: configuration and lifecycle of an external system;
- `docs/runbooks/`: commands, diagnostics, recovery, and operator procedures;
- `docs/security/`: public security model, roles, boundaries, and durable
  limitations;
- `docs/decisions/`: why an architectural decision was made.

Do not duplicate content across categories. Link to the owning page with a
short sentence when another page needs context.

If a page's job does not cleanly map to one category — a cross-product
orientation overview, for example — do not default to shrinking it into
generic prose that restates another page's content. Either give it an
explicit, narrow primary question that no other page answers and keep the
visual that answers it, or formally retire or merge it into the owning page
and update `docs/README.md` so the index no longer points readers at a page
that has quietly become a duplicate.

## Write for the next reader

1. Start from the page contract and the page's existing purpose.
2. Replace stale explanations instead of appending a change report.
3. Preserve the page's terminology, narrative order, and level of abstraction.
4. Describe stable behavior and responsibility. Avoid symbol inventories,
   commit history, test evidence, internal class names, and exhaustive endpoint
   lists unless the page is explicitly an API reference.
5. Do not use source links as evidence or as a substitute for explanation.
   Source links belong only in an explicit developer reference or operational
   runbook, must point to a stable owner, and must not include line numbers.
6. Keep procedures in runbooks and rationale in ADRs. Architecture pages must
   not become deployment checklists or implementation diaries.
7. Use headings, short paragraphs, tables, and lists to make the information
   scannable. Remove repetition before adding more detail.
8. Remove drafting residue such as "verify in code", "while writing", "key
   files", implementation inventories, acceptance evidence, and descriptions
   of what was just changed.

## Handle security information safely

1. Keep public security documentation focused on the security model: assets,
   actors, trust boundaries, controls, responsibilities, and explicitly stated
   residual assumptions.
2. Do not publish exploit recipes, vulnerable function names, unpatched paths,
   credentials, or operational details that materially lower the cost of abuse.
3. Route suspected or active vulnerabilities through the private reporting
   process in `SECURITY.md`. Record remediation evidence in the private issue or
   advisory, not in public reader documentation.
4. Document a control only after verifying it in code or tests. If verification
   is incomplete, report the gap to the user rather than presenting it as fact.

## Use visuals deliberately

Add or update a repository-owned SVG only when relationships, sequence,
boundaries, or ownership are materially clearer visually. Reuse an existing
diagram when it already expresses the concept. A diagram must:

- use reader-facing labels rather than code symbols;
- match the page's abstraction level;
- remain legible at the rendered documentation width;
- add information instead of decorating the page;
- have a title and description for accessibility;
- name every meaningful element with its own label or icon, not only a shared
  legend — a legend is a fallback for relationships that cannot be labeled
  locally, not a substitute for an obvious per-element icon;
- represent every relationship for which the prose relies on the diagram.

Do not require one diagram per page or per change.

### Reuse a shared icon vocabulary instead of a color legend

Fill color alone cannot carry a diagram's meaning: a box distinguished only by
color is unreadable to a reader who has not memorized what each color means,
which is exactly what a color legend admits by existing. If a diagram needs a
text legend to explain its colors, treat that as a signal that meaning
belongs on the node itself — as an icon or label — not in a side key. Prefer
adding icons over adding a legend; color can still group nodes, but it should
never be the only signal.

Before drawing a new icon, search the existing `<symbol id="ico-...">`
definitions across `docs/assets/*.svg` for one that already represents the
same concept, and reuse it — as a `currentColor` path wrapped in a colored
`<g>`, so it still fits the new diagram's palette — rather than inventing a
new shape for something already drawn elsewhere. The same concept should look
the same everywhere it appears (DAST is always the radar icon, a manual or
alternate upload path is always the upload icon, human/AI review is always
the eye icon, and so on). Reused icons make the documentation's visual
vocabulary cumulative instead of every diagram inventing its own private
symbol set that the reader has to relearn each time.

Conciseness in "Write for the next reader" applies to prose duplication, not
to diagram information density. A diagram earns its place by showing
relationships economically; do not strip icons, labeled boundaries, or
data-flow annotations from a diagram merely to make the page around it feel
shorter. Cutting a diagram's content is a content decision with the same bar
as cutting prose, not a side effect of tidying the page.

### When editing an existing diagram

Before replacing or redrawing an SVG that already exists, diff it against its
last committed version (`git show <prior-commit>:<path>`). Treat any dropped
icon, label, boundary annotation, or represented relationship as a regression
that needs an explicit reason — either the underlying behavior actually
changed, or the information moved to a named diagram on another page. Silent
loss is not an acceptable outcome of "keeping it concise."

If the prose that used to accompany a diagram is deleted or moved to another
page, decide the diagram's fate in the same change: it stays here and gets
re-paired with new prose that matches it element by element, it moves with the
content to the page that now owns it, or it is formally retired. Do not leave
that decision implicit — an SVG with no referencing page is a defect, not a
neutral leftover.

### When authoring a new diagram

There is no prior version to diff against, so completeness has to be judged
at authoring time, not caught later by the checks above:

- Search `docs/assets/*.svg` and the pages that reference them for a diagram
  that already covers this relationship at any altitude before drafting a new
  one. Reuse or extend it instead of drafting a competing diagram from scratch.
- Passing the accessibility minimum (`role="img"` with a non-empty `title` and
  `desc`) is necessary, not sufficient. Check the draft against the page's own
  section headings: every section the diagram is meant to accompany should
  have at least one labeled element, icon, or relationship represented in it.
  A section with no counterpart means the diagram is too sparse or scoped
  narrower than the prose — fix one or the other before publishing.
- "Simple for a first version" is not a standing excuse. A newly authored
  diagram has no earlier, richer version to be held accountable to, so make the
  density judgment explicitly now rather than deferring it to a future edit
  that will have nothing to compare against either.

## Review and validate

Review in three passes:

1. **Factual:** every behavior and security property is supported by current
   code, configuration, or tests.
2. **Editorial:** read without the diff and confirm that a new reader can answer
   the page's primary question without knowing the implementation history.
3. **Visual:** compare prose and diagram element by element, then inspect the
   rendered result at documentation width. For an edited diagram, this pass
   includes the diagram-diff check above; for a new diagram, it means checking
   section coverage since there is no prior version to diff.

For broad rewrites, ask for an independent human review of navigation,
terminology, and diagram comprehension. Automated checks cannot establish
reader understanding. Then:

1. validate local Markdown links and anchors;
2. validate every changed SVG as XML;
3. render and visually inspect every changed SVG with `rsvg-convert`;
4. scan for orphaned diagram assets: every file under `docs/assets/*.svg` must
   be referenced by at least one page under `docs/`. Resolve every orphan this
   change produced — re-link it or delete it — before the pass is done;
5. run the repository documentation tests;
6. run `git diff --check`;
7. summarize only reader-facing improvements and unresolved documentation or
   security gaps; never copy the work log into a reader page.
