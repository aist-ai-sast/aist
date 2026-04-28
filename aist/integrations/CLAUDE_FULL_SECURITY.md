# claude-full-security agent analyzer

Claude-driven full-project security analyzer implemented through the generic
`agent-bridge` analyzer type. It runs alongside regular Docker analyzers,
inspects the **deployable runtime code at the scanned revision** (no diff
baseline), and emits high-confidence security findings. Findings can also
carry pre-judged TP/FP verdicts through an `aist_ai_finding_response_v1`
artifact so later AI triage skips them.

This is the static-snapshot counterpart to
[`claude-diff-security`](./CLAUDE_DIFF_SECURITY.md) — both share the same
agent-bridge plumbing, the same import path, and the same post-import
triage skip rule. The only structural difference is the analysis scope.

## How it runs

1. `aist/tasks/pipeline.py` calls `aist.utils.agent_runtime.build_agent_runtime_env`
   before `configure_project_run_analyses`. The helper produces a single
   sidecar dict with the union of keys all agent skills might need
   (`EXCLUDED_PATHS_JSON`, `BASE_COMMIT`, `CLAUDE_DIFF_MAX_*`,
   `AGENT_FULL_MAX_*`); the full skill reads only its own subset.
2. `configure_project_run_analyses` builds the project (clone happens here)
   and runs all regular Docker analyzers. The `claude-full-security` entry
   has `type: agent-bridge`, so `analyzer_runner` skips it inside the builder
   container and records ordinary analyzer outcomes only for containerized
   analyzers.
3. After the builder container finishes, `agent_bridge_runner` reads the
   prepared analyzers config and runs every enabled `type: agent-bridge`
   analyzer through the bridge `/analyze-sync` endpoint.
4. The bridge runs `claude -p` with the
   [`aist-full-security-review` skill](../../.codex/skills/aist-full-security-review/SKILL.md).
   The skill builds a manifest of deployable sub-projects, selects candidate
   files within the configured budget, analyzes them against the security
   categories list, and writes two files into `output_dir`:
   - `claude-full-security_result.json` — Generic Findings Import.
   - `claude-full-security_ai_response.json` — optional
     `aist_ai_finding_response_v1` payload declared in `analyzers.yaml`.
   - On truncation: `claude-full-security_truncated.flag` (one-line reason).
5. The regular `upload_results_internal` flow imports the result file
   (single Test, N Findings keyed by deterministic `unique_id_from_tool`).
6. `aist.utils.analyzer_outcomes.consume_analyzer_outcomes` consumes
   `launch_data.analyzer_outcomes` generically. Degraded required analyzers
   are persisted to `launch_data.analyzer_degraded_reasons`; supported AI
   response artifacts are applied through
   `aist.utils.ai_response_artifact.apply_ai_response_artifact`, which maps
   `uniqueIdFromTool → Finding.id`, creates
   `AISTAIResponse(source=AGENT_ANALYZER)`, and runs
   `sync_ai_finding_responses`.

## Manifest-first scope

The skill does NOT dump every file body into context. The first phase of
methodology is to build a manifest of deployable sub-projects (detected via
`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, deployable
Dockerfiles, etc.) and select the small set of candidate files whose bodies
will actually be read. The total candidate budget is bounded by
`AGENT_FULL_MAX_FILES`, `AGENT_FULL_MAX_BYTES`, and per-file
`AGENT_FULL_MAX_FILE_BYTES`.

This avoids the prompt-size explosion problem on monorepos and keeps the
analyzer applicable to enterprise codebases without runaway cost.

## Required env / settings

| Setting                       | Type     | Default     | Purpose                                                    |
|-------------------------------|----------|-------------|------------------------------------------------------------|
| `CLAUDE_CODE_OAUTH_TOKEN`     | env      | —           | Set on the bridge container; auth for `claude -p`.         |
| `AIST_LOCAL_TRIAGE_BRIDGE_SOCKET` | env  | `/run/codex-bridge/bridge.sock` | Bridge Unix socket path.                  |
| `AIST_LOCAL_TRIAGE_TIMEOUT`   | env      | `1800`      | Bridge timeout (seconds).                                  |
| `AGENT_FULL_MAX_FILES`        | Django   | `1500`      | Hard cap on candidate file count.                          |
| `AGENT_FULL_MAX_BYTES`        | Django   | `8_000_000` | Hard cap on aggregate candidate-body bytes.                |
| `AGENT_FULL_MAX_FILE_BYTES`   | Django   | `200_000`   | Per-file size cap; oversized files are silently dropped.   |
| `AGENT_FULL_MAX_FINDINGS`     | Django   | `50`        | Hard cap on emitted findings.                              |

The four `AGENT_FULL_*` settings have docker-compose env pass-through on
`uwsgi` and `celeryworker` services. Override at deploy time via plain env.

## Per-project overrides

Per-project limits live in the `AISTProject.profile` JSON field under
`agent_analyzers.full_security`:

```json
{
  "agent_analyzers": {
    "full_security": {
      "max_files": 1500,
      "max_bytes": 8000000,
      "max_file_bytes": 200000,
      "max_findings": 50
    }
  }
}
```

Each field is independent — omit a field to fall through to the Django
default. `aist.profile.ProjectProfile.validate_dict` rejects negative,
zero, non-integer, or unknown keys; the resulting error surfaces through
the project-edit form.

## Enable / disable

Both diff and full are registered in
`sast-combinator/sast-pipeline/pipeline/config/analyzers.yaml`:

```yaml
- name: claude-full-security
  type: agent-bridge
  enabled: true
  time_class: slow
  skill_name: aist-full-security-review
  required_result: true
  artifacts:
    ai_response:
      path: claude-full-security_ai_response.json
      format: aist_ai_finding_response_v1
      match_key: unique_id_from_tool
  env:
    - EXCLUDED_PATHS_JSON
    - AGENT_FULL_MAX_FILES
    - AGENT_FULL_MAX_BYTES
    - AGENT_FULL_MAX_FILE_BYTES
    - AGENT_FULL_MAX_FINDINGS
```

Per-pipeline enablement is expressed in **launch config**, not via a
YAML default-selection key. Each project's launch config explicitly lists
the analyzers to run.

## Diff + full simultaneously: no backend mutex

`claude-diff-security` and `claude-full-security` may both be selected in
the same launch config. There is no backend mutex, no project-level
active-run guard, and no `mutually_exclusive_group` field. The launch
config is the single point of analyzer-set decision-making.

Practical implications:

- **Findings overlap is possible.** A vulnerability present at the scanned
  revision will surface under full; if it was introduced by the diff it will
  also surface under diff. Existing dedup handles workflow consistency:
  `unique_id_from_tool` excludes commit hashes and line numbers, so the same
  vulnerability dedups against itself.
- **Cost.** Running both doubles the agent-bridge budget per pipeline.
  Operators may prefer diff for high-frequency CI runs and full for an
  initial onboarding scan or scheduled deep scan.
- **Triage skip is source-based.** Both analyzers create
  `AISTAIResponse(source=AGENT_ANALYZER)` rows; the post-import triage
  queue (`_prepare_auto_push` in `aist/tasks/ai.py`) excludes findings with
  any AGENT_ANALYZER source, so neither analyzer's verdicts get
  re-judged by n8n / local Claude triage.

## Truncation policy

When the project's candidate set exceeds `AGENT_FULL_MAX_FILES` or
`AGENT_FULL_MAX_BYTES`, the skill writes empty result + AI-response files
plus `claude-full-security_truncated.flag` containing the reason
(`files=…>…` or `bytes=…>…`). Pipeline finishes `FINISHED_WITH_WARNINGS`.
No synthetic Info finding is injected.

Per-file overflow (a single file larger than `AGENT_FULL_MAX_FILE_BYTES`)
is silent: the file is dropped from the candidate set without tripping the
truncation marker. A single oversized log-replay or generated file should
not flip the whole run to warnings.

The output cap `AGENT_FULL_MAX_FINDINGS` is a *maximum* on the result file.
Hitting it does NOT trip the truncation marker — write the cap-many
findings and stop.

## Failure handling

| Condition                                          | Pipeline status                                       |
|----------------------------------------------------|-------------------------------------------------------|
| No deployable code detected (e.g. docs-only repo)  | Normal `FINISHED`                                     |
| Anthropic API permanent failure                    | `FINISHED_WITH_WARNINGS` if `required_result: true`   |
| Truncation flag present in output_dir              | `FINISHED_WITH_WARNINGS`                              |
| AI response artifact application raises            | Logged + pipeline continues                           |
| Bridge socket unavailable                          | `FINISHED_WITH_WARNINGS` if `required_result: true`   |

## Skipping post-import triage

Findings with analyzer-produced verdicts are filtered out of the post-import
triage queue in `aist/tasks/ai.py:_prepare_auto_push` via
`~Q(aist_ai_responses__source_response__source=AGENT_ANALYZER)`. The skip
rule is source-based — adding a new agent analyzer inherits this behavior
automatically.
