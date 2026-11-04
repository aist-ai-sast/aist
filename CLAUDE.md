@AGENTS.md

# Claude Code — Additions

## Review subagents

The review is described in `AGENTS.md` "Review before commit". The reviewers are the
subagents in `.claude/agents/` that `.sdlc/scope.yaml` maps each reviewer name to; dispatch
them in parallel with the brief's path. The PostToolUse hook names the reviewers an edited
file routes to, so the list stays in view across a long session.

When a code change alters something a page under `docs/` describes — a product workflow,
tenant boundary, integration, task, runtime service, deployment or security control — also
run `aist-architecture-docs-maintainer` in update mode; the pages it changes are then
reviewed by the `docs` reviewer like any other change.

**Before committing changes to `aist/auth_backends.py`, `aist/members/`,
`aist/api/tokens.py`, `aist/api/account.py`, `aist/api/organizations.py`, or any new
authentication/authorization logic:** the routed reviewers are diff-scoped — they will not catch cross-endpoint systemic gaps (e.g. "does every mutating
endpoint honor token scope") or issues that only manifest under concurrency. For changes in
this category, also run `/aist-security-check` (still diff-scoped but repeats the fuller
checklist).

For an even deeper pass, `.codex/skills/aist-diff-security-review/SKILL.md` documents a
rigorous sink/source/trust-boundary review methodology (it names race/TOCTOU and
state-transition-guard vulnerability classes explicitly) — that file is wired to the SAST
pipeline's own analyzer (real `source_path`/`output_path`/runtime-sidecar args, writes
Generic-Findings-Import JSON for `aist-triage-bridge`) and must NOT be edited or invoked
as-is for local dev review; its inputs won't exist outside the pipeline. Read it for the
*reasoning approach* and apply that thinking manually in the current session instead of
running it as a skill.

## How to invoke skills

When the user calls `/skill-name`, read the SKILL.md from the skills table in AGENTS.md
and follow its instructions exactly.
