---
name: aist-intake-diff-review
description: Vet an UPDATE to third-party source code (e.g. a new drop of an external plugin) before integrating it. Reviews the git diff between the previously-accepted revision and HEAD under an inverted threat model — the author of the update is a potential adversary who may have slipped something malicious into the delta. Flags diff-introduced undeclared outbound URLs, obfuscated/encoded blobs, dynamic code execution, install/import-time side effects, secret/environment harvesting, persistence, backdoor triggers, dependency manipulation, embedded binaries, and destructive operations. Emits HIGH-confidence malicious-behaviour findings plus lower-confidence "review-required" indicators as a deterministic Generic Findings Import JSON plus an AISTAIFindingResponse-shaped sibling file. Used by the SAST pipeline's `claude-intake-diff` analyzer through aist-triage-bridge `/analyze-sync`.
---

Read `.codex/skills/aist-intake-diff-review/SKILL.md` and follow the instructions exactly.
</content>
