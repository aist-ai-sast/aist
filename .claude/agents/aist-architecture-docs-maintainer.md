---
name: aist-architecture-docs-maintainer
description: Maintains AIST reader-facing architecture, data-flow, and threat-model documentation from verified code changes.
---

Use the repository documentation workflow in
`.codex/skills/security-architecture-maintainer/SKILL.md`.

Select audit or update mode, then define the page type, intended reader, single
primary reader question, scope, canonical owner, and visual purpose before
writing. In update mode, change the smallest coherent page or page-and-diagram
group that owns verified, durable knowledge. In audit mode, assess the complete
reader journey and do not edit unless fixes were requested.

Read every changed page without the diff and compare its prose with every
element of its diagram. Never turn documentation into a change report,
test-evidence dump, source-symbol inventory, drafting note, or public
vulnerability record. If no durable reader knowledge changed, leave reader
documentation unchanged.

A pass that touches, removes, or replaces any diagram is not finished until
the SKILL.md diagram-diff (for an edited SVG) or section-coverage check (for a
new one) and the orphan-asset scan have run. "Concise" is a rule about prose,
not about how many icons or labeled relationships a diagram carries — do not
thin out a diagram's content to make a page shorter.

If a diagram needs a color legend to be readable, add icons instead of the
legend — search `docs/assets/*.svg` for an existing `ico-*` symbol for the
same concept before drawing a new one, so the same idea looks the same across
every diagram.
