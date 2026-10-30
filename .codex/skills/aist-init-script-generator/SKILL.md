---
name: aist-init-script-generator
description: Generate a project-specific AIST initialization script by preserving required git-clone and environment export logic from default_script.py and adapting the rest to the real build/bootstrap flow of a target repository for SAST analysis.
---

# AIST Skill: Generate Project-Specific Init Script

## Purpose

Generate a **project-specific initialization script** for SAST analysis based on:

* the target repository (input)
* AIST default initialization logic (local reference)
* actual build/setup logic of the target project

This skill adapts generic AIST initialization behavior to a real project.

---

## How the script runs (runtime contract)

Read this before designing anything — every rule below follows from it.

* The script is a **bash** script. It is not executed but **sourced**
  (`source ./project_config.sh`) by
  `sast-combinator/sast-pipeline/Dockerfiles/builder/builder-entrypoint.sh`,
  which runs under `set -e`. After the script returns, the same shell launches
  every analyzer (`run_inside_builder.py`).
* Therefore **any `exit` (including `exit 0`) and any unguarded failing command
  ends the whole builder: no analyzer runs, and the pipeline silently loses all
  results.** Only the git clone/checkout block from `default_script.py` may exit.
* The builder image is `ubuntu:22.04` (root, system `python3` 3.10 that runs the
  analyzer launcher). The OS will usually differ from the project's own build
  image; that is expected — install the exact toolchain versions yourself.
* The project's VPN integration (if configured) is active during the run, so
  internal hosts the real build uses (e.g. `gitlab.nxvms.dev`,
  `artifactory.us.nxteam.dev`) are reachable.
* Available environment: `REPO_URL` (clone URL with the SCM integration
  credential embedded), `PROJECT_NAME`, `PROJECT_VERSION`, `FORCE_REBUILD`,
  `LOG_LEVEL`, plus any launch-config variables. Nothing else is guaranteed —
  e.g. `DEPLOY_USER`/`DEPLOY_TOKEN` from the project's CI are normally absent.
* Analyzers run in **separate containers** that mount the workspace. Only what
  is inside `$DEV_DIR` (e.g. `node_modules`, a project `.venv`, build dirs) is
  visible to them; tool caches outside it are not.
* C/C++ builder-type analyzers (e.g. infer) run only when the script exports
  `NON_COMPILE_PROJECT=0` and `COMPILE_COMMANDS_PATH` pointing at an existing
  `compile_commands.json`; if that variable is set but the file is missing, the
  entrypoint itself exits 1.

---

## Inputs

* `project_id` **(required)**
  AIST project ID. Used to persist the generated script in the platform database.

* `project_version_id` **(required)**
  ID of the `GIT_BRANCH` project version the script is generated for. `target_repo_path`
  is a clone of exactly that branch, and the script is bound to exactly this version:
  its pipelines, and the commits they resolve, run it. If it is missing, stop and
  report the error. Do not persist to the project without a version.

* `target_repo_path` **(required)**
  Absolute path to the repository that must be prepared for build and SAST analysis.

* `failure_log_path` **(optional)**
  Path to a failed run log. Used only as supporting diagnostic evidence.

---

## Fixed reference

Default script (always local to AIST):

`aist/default_script.py`

---

## Critical requirements (MANDATORY)

The generated script MUST preserve:

1. **git clone logic from default_script.py**
2. **export of ALL required environment variables from default_script.py**

These are **core AIST behaviors** and must not be removed, simplified, or replaced.

Only those two parts are reused. Everything after the git checkout block is
**replaced** by the project's real build flow: the generic package-manager
blocks of `default_script.py` (Node 20, npm/pnpm/yarn/poetry/pip auto-detection)
must not be carried over.

3. **Nothing after the git checkout block may abort the run.** Every
   project-specific step is guarded (`cmd || echo "[TAG][WARN] ..."`), and the
   script never calls `exit` outside the git block (see the runtime contract).

---

## Core principles

* **Target repo = source of truth** for build/setup
* **default_script.py = source of truth** for AIST behavior
* **failure log = optional hint**, not design driver

---

## Workflow

### Phase 1 — Inspect target repository

Analyze the real project at `target_repo_path`.

At minimum inspect:

* `package.json`
* lockfiles (`pnpm-lock.yaml`, `package-lock.json`, `yarn.lock`)
* Dockerfiles
* docker-compose / compose
* Makefile / scripts
* CI configs (if relevant)
* dependency manifests (`requirements*.txt`, `pyproject.toml`)
* any bootstrap/build scripts

Goal:
Understand how the project is **actually built and prepared**.

Choose the source of truth explicitly:

* When several build paths exist (shipping Dockerfile, EKS/kaniko Dockerfile,
  CI jobs, release scripts), take the one that builds **this branch** — check
  CI rules (e.g. `cloud-*` branches use a different Dockerfile) and CI variables
  that override Dockerfile args (e.g. `NODE_IMAGE`, `GO_VERSION`).
* Record the **exact** toolchain and package-manager versions it uses (base
  image tag, `.nvmrc`, `engines`, `packageManager`, `go.mod` / CI `GO_VERSION`,
  `.python-version`, pinned `npm`/`pnpm`/`uv`), the exact install commands and
  flags (`npm ci` vs `npm install`, lifecycle scripts enabled, `--frozen`, uv
  groups), and the registry/index configuration the build uses (`.npmrc`,
  `pip.conf`, `PIP_INDEX_URL`, private Go modules + how credentials are passed).
* Note which steps only produce deploy artifacts (bundles, images) — those are
  not needed for SAST.

---

### Phase 2 — Inspect AIST default logic

Inspect:

`aist/default_script.py`

Extract:

* git clone logic (**must be preserved**)
* environment variable exports (**must be preserved**)
* reusable generic setup steps
* assumptions about project structure

---

### Phase 3 — Detect mismatch

Compare:

* default_script behavior
  vs
* actual target project requirements

If `failure_log_path` is provided:

* use it only to confirm issues

Identify:

* outdated assumptions
* incorrect dependency installation logic
* wrong package manager usage
* broken Docker/build assumptions
* missing or invalid steps

---

### Phase 4 — Derive adapted flow

Design a new setup flow that:

* preserves git clone logic
* preserves env variable export
* reuses valid AIST logic
* replaces only broken/project-specific parts
* matches real build flow of target repo
* imitates Docker build behavior where needed
* avoids unnecessary runtime startup
* prepares project for SAST (not full deploy)

Rules for the adapted flow:

* **Same versions, same commands, same config.** Install the exact toolchain
  versions found in Phase 1 (e.g. Node via `n <version>`, Python via
  `uv venv --python <version>`, Go `<series>` latest patch from go.dev — matching
  the `golang:<series>` image). Run the same install commands with the same
  flags. Never invent steps the real build does not have, and never drop steps
  it has (lifecycle scripts, patches, extra package dirs) unless they only build
  deploy artifacts.
* **Use the project's own registries.** Keep `.npmrc` / `pip.conf` / index URLs
  as the build uses them; internal hosts are reachable via VPN. Do not swap in
  public registries.
* **Private dependencies:** use the variables the real build uses when they are
  set; otherwise derive the credential from `REPO_URL` for the same SCM host and
  pass it via `GIT_CONFIG_COUNT/KEY_0/VALUE_0` (`insteadOf`) env — never write it
  to disk or print it.
* **Do not touch the builder's system Python.** Python dependencies go into a
  project venv inside `$DEV_DIR` (e.g. `$DEV_DIR/.venv`), created with the
  project's Python version.
* **Compiled languages:** actually compile (Go `go build`, C/C++ configure +
  build). For C/C++ generate `compile_commands.json`
  (`-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`) and export `NON_COMPILE_PROJECT=0` and
  `COMPILE_COMMANDS_PATH` **only if the file exists**.
* **Do not create scanned noise:** skip bundle/dist builds; if installed
  dependency directories (`node_modules`, `bower_components`, `.venv`) are not
  covered by the project profile's excluded paths, say so in the summary.

---

### Phase 5 — Generate final script

Produce a **full bash script** that:

* is project-specific
* deterministic
* CI-friendly
* non-interactive
* has clear logging (`[TAG] ...`, failures as `[TAG][WARN] ...`)
* never aborts after the git block — a failed step warns and the run continues,
  so analyzers still scan the sources
* documents in comments which file/line of the target repo each step mirrors

---

### Phase 6 — Verify (MANDATORY before persisting)

A script that was never run is not done.

1. Static: `bash -n` passes; no `exit` outside the git block; every
   project-specific command is guarded.
2. Dynamic: run the script exactly as the builder does — `set -e`, then
   `source` it — inside the project's builder image
   (`project-<product name>-builder:latest`) when it exists, otherwise
   `ubuntu:22.04`, with `REPO_URL`, `PROJECT_NAME`, `PROJECT_VERSION` set,
   a fresh empty directory mounted at `/workspace`, and the entrypoint
   replaced by a marker instead of the analyzer launch. Check that it reaches
   the end, and that the expected result exists (dependency dirs/venv, the
   toolchain versions, a successful compile, `compile_commands.json`).
3. If verification cannot reach an internal host because no VPN is available
   in your environment, do **not** change the registry to work around it —
   report the step as unverified instead.
4. Clean up the workspace and containers you created.

---

## Persistence (MANDATORY)

1. Use `docker compose` to persist the generated script to the platform database.
2. Store the script and bind it to the version through Django ORM. The version is
   looked up inside the project, so an ID from another project fails instead of binding:
   ```
   docker compose exec uwsgi python manage.py shell -c "
   from aist.models import AISTProjectScript, AISTProjectVersion
   version = AISTProjectVersion.objects.get(pk=<project_version_id>, project_id=<project_id>)
   script, created = AISTProjectScript.create_for_version(
       content='''<generated script content>''',
       version=version,
   )
   print(f'Script {script.id} {"created" if created else "reused"}, bound to version {version.id} ({version.version})')
   "
   ```
   `create_for_version` accepts only a `GIT_BRANCH` version and changes no other version.
   Never call `get_or_create_for_project` directly: a revision that no version uses
   never runs.
3. Do NOT write the script to a file on disk as primary output.
4. The chat response is secondary to the persisted database state.

---

## Output format (STRICT)

### 1. Target repo analysis

How the project is built (based on sources)

### 2. AIST logic extraction

* git clone logic (preserved)
* env exports (preserved)
* reusable vs outdated parts

### 3. Mismatch summary

Why default script fails

### 4. New script design

What was adapted and why

### 5. Final script (FULL CODE)

### 6. Verification result

What was run, what it produced (versions, deps, build), and anything unverified.

### 7. Persist to database

Save script via `docker compose exec` as described in Persistence section above.

### 8. Summary

What was persisted: the script ID, and the version ID and branch name it is bound to.

---

## Constraints

* DO NOT remove git clone logic
* DO NOT remove env export logic
* DO NOT generate generic script
* DO NOT rely only on logs
* DO NOT treat AIST repo as target project
* DO NOT over-engineer
* DO NOT `exit` or leave a failing command unguarded after the git block
* DO NOT replace the project's registries/indexes with public ones
* DO NOT install into the builder's system Python
* DO NOT persist a script that was not verified (Phase 6)

---

## Success criteria

* Script tailored to target repo
* AIST git/env logic preserved
* Project-specific logic correctly adapted
* Script prepares repo for SAST with the same toolchain versions, commands and
  registries as the project's real build
* Verified by an actual sourced run (Phase 6); no step after the git block can
  abort the builder
* Works in CI
* `AISTProjectScript` record created or reused in the database for the given `project_id`
* The `GIT_BRANCH` version `project_version_id` now has `script` set to that record; no other version changed

---

## Example

```id="aist-skill-example"
Use this skill:

project_id=42
project_version_id=108
target_repo_path="/tmp/aist/projects/my-project/codex-analysis"
```

Result:

* project-specific init script generated
* persisted as AISTProjectScript in the database and bound to version 108
