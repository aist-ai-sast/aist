#!/usr/bin/env bash
# PostToolUse reminder (Edit|Write): the "automatic" subagent delegation described in
# CLAUDE.md is advisory only (no hook previously enforced it), which is exactly how real
# security bugs shipped unreviewed in past commits. This does not block anything — it just
# makes sure the acting session can't lose track of "a checker still needs to run" across a
# long conversation.
set -euo pipefail

input="$(cat)"
file_path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')"

if [ -z "$file_path" ]; then
  exit 0
fi

reminders=()

case "$file_path" in
  */aist/test/*|*/aist/migrations/*)
    ;;
  */aist/*|*/aist_site/*|*/context_extractor_service/*)
    reminders+=("aist-security-checker (.claude/agents/aist-security-checker.md) — touches aist/, aist_site/, or context_extractor_service/")
    ;;
esac

case "$file_path" in
  */aist/api/*)
    reminders+=("aist-api-reviewer (.claude/agents/aist-api-reviewer.md) — touches aist/api/")
    ;;
esac

case "$file_path" in
  */client-ui/src/pages/*|*/client-ui/src/components/*|*/client-ui/src/lib/*)
    case "$file_path" in
      *.test.ts|*.test.tsx) ;;
      *) reminders+=("aist-ui-security-checker (.claude/agents/aist-ui-security-checker.md) — touches client-ui/src/pages, components, or lib") ;;
    esac
    ;;
esac

case "$file_path" in
  */aist/models.py|*/aist/migrations/*)
    reminders+=("aist-migration-validator (.claude/agents/aist-migration-validator.md) — touches aist/models.py or aist/migrations/")
    ;;
esac

if [ ${#reminders[@]} -eq 0 ]; then
  exit 0
fi

lines="SECURITY REMINDER: $file_path is a security-sensitive path. Before finishing this task or committing, invoke (if not already run against this diff):"
for r in "${reminders[@]}"; do
  lines="$lines"$'\n'"- $r"
done

jq -n --arg ctx "$lines" '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $ctx}}'
