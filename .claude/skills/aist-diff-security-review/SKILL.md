---
name: aist-diff-security-review
description: Analyze the git diff between the previous successful pipeline's commit and the current HEAD on the same branch. Identify security regressions across SSRF, DoS, secrets, auth/IDOR, injections, traversal, deserialization, XSS/CSRF/CORS/redirect, sensitive-data exposure, weak crypto, mass assignment, TOCTOU, and Docker/IaC/CI regressions. Emit a deterministic Generic Findings Import JSON plus an AISTAIFindingResponse-shaped sibling file. Used by the SAST pipeline's `claude-diff-security` analyzer through aist-triage-bridge `/analyze-sync`.
---

Read `.codex/skills/aist-diff-security-review/SKILL.md` and follow the instructions exactly.
