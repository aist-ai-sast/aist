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

## Inputs

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

The task is to **adapt**, not rewrite.

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

---

### Phase 5 — Generate final script

Produce a **full Python script** that:

* is project-specific
* deterministic
* CI-friendly
* non-interactive
* has clear logging
* fails fast

Default output path:

`{target_repo_path}/project_init_script.py`

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

### 6. Save action

Write script to target repo root

### 7. Usage

How to run

---

## Constraints

* DO NOT remove git clone logic
* DO NOT remove env export logic
* DO NOT generate generic script
* DO NOT rely only on logs
* DO NOT treat AIST repo as target project
* DO NOT over-engineer

---

## Success criteria

* Script tailored to target repo
* AIST git/env logic preserved
* Project-specific logic correctly adapted
* Script prepares repo for SAST
* Works in CI

---

## Example

```id="aist-skill-example"
Use this skill:

target_repo_path="/Users/butkevichveronika/work/nx-maps-ui"
```

Result:

* project-specific init script generated
* saved to target repo root
