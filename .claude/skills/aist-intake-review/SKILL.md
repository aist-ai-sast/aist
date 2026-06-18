---
name: aist-intake-review
description: Vet third-party source code (e.g. an external plugin) before integrating it into our codebase or running it on our infrastructure. Inverted threat model — the author is a potential adversary who may have hidden malicious behaviour. Scans the whole deployable revision for undeclared outbound URLs, obfuscated/encoded blobs, dynamic code execution, install/import-time side effects, secret/environment harvesting, persistence, backdoor triggers, dependency manipulation, embedded binaries, and destructive operations. Emits HIGH-confidence malicious-behaviour findings plus lower-confidence "review-required" indicators as a deterministic Generic Findings Import JSON plus an AISTAIFindingResponse-shaped sibling file. Used by the SAST pipeline's `claude-intake-review` analyzer through aist-triage-bridge `/analyze-sync`.
---

Read `.codex/skills/aist-intake-review/SKILL.md` and follow the instructions exactly.
</content>
