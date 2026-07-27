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
  appeared in the same diff.

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
- have every meaningful element explained by nearby text or a legend;
- represent every relationship for which the prose relies on the diagram.

Do not require one diagram per page or per change.

## Review and validate

Review in three passes:

1. **Factual:** every behavior and security property is supported by current
   code, configuration, or tests.
2. **Editorial:** read without the diff and confirm that a new reader can answer
   the page's primary question without knowing the implementation history.
3. **Visual:** compare prose and diagram element by element, then inspect the
   rendered result at documentation width.

For broad rewrites, ask for an independent human review of navigation,
terminology, and diagram comprehension. Automated checks cannot establish
reader understanding. Then:

1. validate local Markdown links and anchors;
2. validate every changed SVG as XML;
3. render and visually inspect every changed SVG with `rsvg-convert`;
4. run the repository documentation tests;
5. run `git diff --check`;
6. summarize only reader-facing improvements and unresolved documentation or
   security gaps; never copy the work log into a reader page.
