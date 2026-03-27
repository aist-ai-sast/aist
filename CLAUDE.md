@AGENTS.md

# Claude Code — Additions

## Autonomous agents

Claude Code will automatically delegate security and API checks to subagents in
`.claude/agents/` — no explicit invocation needed. They trigger on relevant file changes:

- `aist-security-checker` — after any edits to `aist/`, `context_extractor_service/`,
  `sast-pipeline/`
- `aist-api-reviewer` — after any edits to `aist/api/`

## How to invoke skills

When the user calls `/skill-name`, read the SKILL.md from the skills table in AGENTS.md
and follow its instructions exactly.
