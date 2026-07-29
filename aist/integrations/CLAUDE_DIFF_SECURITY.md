# claude-diff-security agent analyzer

Claude-driven analyzer implemented through the generic `agent-bridge`
analyzer type. It runs alongside regular Docker analyzers, compares the cloned
project at `BASE_COMMIT..HEAD`, and emits security findings introduced by the
diff. Findings can also carry pre-judged TP/FP verdicts through an
`aist_ai_finding_response_v1` artifact so later AI triage skips them.

For the static-snapshot counterpart (no diff baseline) see
[`CLAUDE_FULL_SECURITY.md`](./CLAUDE_FULL_SECURITY.md). Both share the same
agent-bridge plumbing and may run together in the same pipeline.

## How it runs

1. `aist/tasks/pipeline.py` calls `aist.utils.diff_baseline.build_diff_env`
   before `configure_project_run_analyses`. That helper does one DB lookup:
   the previous terminal-success pipeline's commit on the same resolved
   branch. It produces `BASE_COMMIT`, `EXCLUDED_PATHS_JSON`,
   `CLAUDE_DIFF_MAX_FILES`, and `CLAUDE_DIFF_MAX_BYTES`.
2. `configure_project_run_analyses` builds the project (clone happens here)
   and runs all regular Docker analyzers. The `claude-diff-security` entry has
   `type: agent-bridge`, so `analyzer_runner` skips it inside the builder
   container and records ordinary analyzer outcomes only for containerized
   analyzers.
3. After the builder container finishes, `agent_bridge_runner` reads the
   prepared analyzers config and runs every enabled `type: agent-bridge`
   analyzer through the bridge `/analyze-sync` endpoint over the Unix socket
   `$AIST_LOCAL_TRIAGE_BRIDGE_SOCKET` (default
   `/run/claude-bridge/bridge.sock`).
4. The bridge runs `claude -p` with the
   [`aist-diff-security-review` skill](../../.codex/skills/aist-diff-security-review/SKILL.md).
   The skill resolves the BASE fallback chain (see below), computes the
   diff, analyzes each hunk against the security categories list, and
   writes two files into `output_dir`:
   - `claude-diff-security_result.json` — Generic Findings Import.
   - `claude-diff-security_ai_response.json` — optional
     `aist_ai_finding_response_v1` payload declared in `analyzers.yaml`.
   - On truncation: `claude-diff-security_truncated.flag` (one-line reason).
5. The regular `upload_results_internal` flow imports the result file
   (single Test, N Findings keyed by deterministic
   `unique_id_from_tool`).
6. `aist.utils.analyzer_outcomes.consume_analyzer_outcomes` consumes
   `launch_data.analyzer_outcomes` generically. Degraded required analyzers
   are persisted to `launch_data.analyzer_degraded_reasons`; supported AI
   response artifacts are applied through
   `aist.utils.ai_response_artifact.apply_ai_response_artifact`, which maps
   `uniqueIdFromTool → Finding.id`, creates
   `AISTAIResponse(source=AGENT_ANALYZER)`, and runs
   `sync_ai_finding_responses`.

## BASE_COMMIT 3-level fallback

The skill resolves BASE in this order, picking the first that yields a
reachable commit:

| Level | Source                                                                                                                                               |
|-------|------------------------------------------------------------------------------------------------------------------------------------------------------|
| L1    | `$BASE_COMMIT` (env var, populated by `build_diff_env` from the previous terminal-success pipeline on the same resolved branch).                     |
| L2    | `git log --since='14 days ago' --reverse --format='%H' | head -1` against the cloned repo.                                                          |
| L3    | `git rev-list --max-parents=0 HEAD | head -1` (very first commit). Diff covers the whole project history into HEAD; expect this to trip truncation. |

`HEAD` is always `git rev-parse HEAD` inside the cloned repo.

## Required env / settings

- `CLAUDE_CODE_OAUTH_TOKEN` — set on the bridge container; auth for
  `claude -p`. The diff analyzer does NOT introduce any new secret.
- `AIST_LOCAL_TRIAGE_BRIDGE_SOCKET` — Unix socket path. The Compose deployment
  uses `/run/claude-bridge/bridge.sock`.
- `AIST_LOCAL_TRIAGE_TIMEOUT` — seconds. Compose defaults to 10,800; the
  standalone Django fallback is 1,800. The bridge enforces this limit and the
  synchronous client allows an additional 60 seconds for the HTTP exchange.
- `CLAUDE_DIFF_MAX_FILES` — maximum changed files analyzed per run. Compose
  defaults to 1,500; the standalone Django fallback is 200.
- `CLAUDE_DIFF_MAX_BYTES` — maximum bytes of unified diff text. Compose
  defaults to 6,000,000; the standalone Django fallback is 1,000,000.

## Enable / disable

Toggle in `sast-combinator/sast-pipeline/pipeline/config/analyzers.yaml`:

```yaml
- name: claude-diff-security
  type: agent-bridge
  enabled: false  # set to true to run; false to skip
  required_result: true
  artifacts:
    ai_response:
      path: claude-diff-security_ai_response.json
      format: aist_ai_finding_response_v1
      match_key: unique_id_from_tool
```

`time_class: slow` — the analyzer is excluded from `--max-time-class fast`
runs.

## Truncation policy

When `CLAUDE_DIFF_MAX_FILES` or `CLAUDE_DIFF_MAX_BYTES` is exceeded the
skill writes empty result + AI response files plus
`claude-diff-security_truncated.flag` containing the reason. Pipeline
finishes `FINISHED_WITH_WARNINGS`. No synthetic Info finding is injected.

## Failure handling

| Condition                                          | Pipeline status              |
|----------------------------------------------------|------------------------------|
| Diff empty (no changes after exclusion)            | Normal `FINISHED`            |
| Anthropic API permanent failure                    | `FINISHED_WITH_WARNINGS` if `required_result: true` |
| BASE unreachable + L2/L3 fallback empty            | Normal `FINISHED`            |
| Truncation flag present in output_dir              | `FINISHED_WITH_WARNINGS`     |
| AI response artifact application raises            | Logged + pipeline continues  |
| Bridge socket unavailable                          | `FINISHED_WITH_WARNINGS` if `required_result: true` |

## Known limitations (v1)

- `unique_id_from_tool` is computed without `line` and without commit
  hashes so the same vulnerability re-surfacing on a different line
  dedups correctly. A renamed file still looks like a new finding;
  cross-rename mapping is future work.
- Shallow clones of the project may force the BASE chain into L3 (first
  commit available); expect more truncation in that case.
- A force-push that drops `BASE_COMMIT` from history is detected via
  `git cat-file -e $BASE` and falls through to L2/L3 transparently.

## Skipping post-import triage

Findings with analyzer-produced verdicts are filtered out of the post-import
triage queue in `aist/tasks/ai.py:_prepare_auto_push` via
`~Q(aist_ai_responses__source_response__source=AGENT_ANALYZER)`.
The analyzer-source verdict wins; do not re-run them through n8n / local
Claude triage.
