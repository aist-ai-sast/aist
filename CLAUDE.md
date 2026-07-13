@AGENTS.md

# Claude Code — Additions

## Autonomous agents

Claude Code will automatically delegate security and API checks to subagents in
`.claude/agents/` — no explicit invocation needed. They trigger on relevant file changes:

- `aist-security-checker` — after any edits to `aist/`, `context_extractor_service/`,
  `sast-pipeline/`
- `aist-api-reviewer` — after any edits to `aist/api/`
- `aist-ui-security-checker` — after any edits to `client-ui/src/pages/`,
  `client-ui/src/components/`, `client-ui/src/lib/`
- `aist-migration-validator` — after any edits to `aist/models.py` or `aist/migrations/`

This trigger list is advisory, not enforced by a hook — a session can lose track of it
across a long conversation or when work is delegated to a sub-agent that doesn't inherit
this file. Do not treat "the agent didn't flag anything" as equivalent to "the agent ran."
If you are not certain a relevant checker actually ran against the current diff, run it
explicitly before finishing.

**Before merging/committing changes to `aist/auth_backends.py`, `aist/members/`,
`aist/api/tokens.py`, `aist/api/account.py`, `aist/api/organizations.py`, or any new
authentication/authorization logic:** the fast checkers above are pattern-based and diff-
scoped — they will not catch cross-endpoint systemic gaps (e.g. "does every mutating
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
