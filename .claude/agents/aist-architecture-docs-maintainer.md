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
