---
name: security-architecture-maintainer
description: Keep AIST reader-facing architecture, data-flow, and threat-model documentation current after verified code changes. Use when a diff changes a product workflow, tenant boundary, integration, task, runtime service, deployment, or security control.
---

# AIST Documentation Maintainer

1. Read `AGENTS.md`, `README.md`, and `docs/README.md`.
2. Trace the changed behavior through API/UI, models, tasks, runtime, and tests.
3. Update only the affected canonical page(s); never add assumptions, plans, or agent evidence to reader pages.
4. Give each changed page one distinct, repository-owned SVG when a visual improves comprehension. Use existing UI provider icons for named technologies.
5. Render every changed SVG with `rsvg-convert`, inspect it visually, validate XML and Markdown links, then run `git diff --check`.
6. If a change affects assets, trust boundaries, controls, or threats, update the linked security page and threat register. Report an unverified gap rather than documenting it as a control.
