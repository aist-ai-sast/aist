---
name: sast-analyzer-add
description: Add a new SAST analyzer to the sast-pipeline orchestrator with the required Dockerfile, analyze.sh, analyzer config entry, and test coverage.
---

# sast-analyzer-add

Add a new SAST analyzer to the sast-pipeline orchestrator.
All four components are required: Dockerfile, analyze.sh, config entry, test.

## Inputs

- `name` (required): analyzer name in lowercase-hyphen format (e.g., `my-tool`)
- `image` (required): Docker image name (e.g., `sast-my-tool`)
- `output_type` (required): scan result format accepted by the platform importer
  (e.g., `SARIF`, `Semgrep JSON Report`, `Generic Findings Import`)
- `languages` (optional): comma-separated list of languages this analyzer supports
  (e.g., `python,javascript`). Omit if language-agnostic.
- `time_class` (optional): `fast`, `medium`, or `slow`. Default: `medium`.

## Step-by-step workflow

### Step 1 — Read existing patterns

Before writing anything:
1. Read `sast-combinator/sast-pipeline/pipeline/config/analyzers.yaml`
   — understand the config structure and find an analyzer similar to the new one.
2. Read the Dockerfile and `analyze.sh` of the most similar existing analyzer in
   `sast-combinator/sast-pipeline/Dockerfiles/<similar-analyzer>/`.
3. Read `sast-combinator/sast-pipeline/pipeline/defect_dojo/utils.py`
   — check `resolve_scan_type()` to understand how `output_type` maps to importer names.

### Step 2 — Create Dockerfile

Create `sast-combinator/sast-pipeline/Dockerfiles/<name>/Dockerfile`:

```dockerfile
# Use a minimal base image appropriate for the tool
FROM python:3.12-slim
# or debian:bookworm-slim, ubuntu:22.04, etc.

# Install the analyzer tool
RUN apt-get update && apt-get install -y --no-install-recommends \
    <tool-package> \
    && rm -rf /var/lib/apt/lists/*

# Copy the analysis script
COPY analyze.sh /analyze.sh
RUN chmod +x /analyze.sh

# Switch to non-root user (required)
RUN useradd -m analyzer
USER analyzer

ENTRYPOINT ["/analyze.sh"]
```

**Security rules for Dockerfile:**
- [ ] Base image pinned to specific version tag (not `latest`)
- [ ] `apt-get` lists cleaned (`rm -rf /var/lib/apt/lists/*`)
- [ ] Runs as non-root user at the end
- [ ] No hardcoded secrets or tokens
- [ ] No `--privileged` in RUN commands

### Step 3 — Create analyze.sh

Create `sast-combinator/sast-pipeline/Dockerfiles/<name>/analyze.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

INPUT_PATH="$1"    # path to source code (from builder volume)
OUTPUT_DIR="$2"    # where to write results
OUTPUT_FILE="$3"   # output filename (e.g., my_tool.sarif)

OUTPUT_PATH="$OUTPUT_DIR/$OUTPUT_FILE"

echo "[my-tool] Starting analysis of $INPUT_PATH"

# Run the analyzer
my-tool-command \
    --input "$INPUT_PATH" \
    --output "$OUTPUT_PATH" \
    --format sarif  # or whatever format the tool supports

if [ ! -f "$OUTPUT_PATH" ]; then
    echo "[my-tool] ERROR: output file not created at $OUTPUT_PATH"
    exit 1
fi

echo "[my-tool] Done. Output: $OUTPUT_PATH"
```

**Rules:**
- Always use `set -euo pipefail` — fail fast on any error.
- Arguments are positional: `$1` = input path, `$2` = output dir, `$3` = output filename.
- Always verify that the output file was actually created before exiting.
- Use `echo "[<tool-name>] ..."` prefix for all log messages.

### Step 4 — Add config entry

Add to `sast-combinator/sast-pipeline/pipeline/config/analyzers.yaml`:

```yaml
- name: <name>
  type: simple                    # use "simple" unless language-specific containers needed
  image: <image>
  enabled: true
  time_class: <fast|medium|slow>
  output_type: "<output_type>"
  result_file: "<name>.sarif"     # or .json depending on output_type
  language:                       # omit if language-agnostic
    - python
    - javascript
```

If `output_type` is not yet handled by `resolve_scan_type()` in `utils.py`, add a mapping:
```python
# In pipeline/defect_dojo/utils.py, resolve_scan_type()
if output_type == "My Tool Output":
    return "My Tool Importer Name"  # must match exact platform importer name
```

### Step 5 — Write test

Add a test in `sast-combinator/sast-pipeline/tests/`:

```python
# test_<name>_analyzer.py

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "<name>"

def test_<name>_report_parsed():
    """
    Scenario: analyzer produces a SARIF report; pipeline processes it and uploads to platform.
    Expected: findings are extracted from the report with correct file paths and severities.
    """
    report_path = FIXTURES_DIR / "sample_output.sarif"
    assert report_path.exists(), "Missing test fixture"

    # Load and validate the fixture report structure
    import json
    with open(report_path) as f:
        report = json.load(f)

    assert "runs" in report
    runs = report["runs"]
    assert len(runs) > 0
    assert "results" in runs[0]

def test_<name>_empty_report_handled():
    """
    Scenario: analyzer finds no issues; pipeline handles empty output without crashing.
    """
    report_path = FIXTURES_DIR / "empty_output.sarif"
    # Verify empty SARIF is valid
    import json
    with open(report_path) as f:
        report = json.load(f)
    assert report.get("runs", [{}])[0].get("results", []) == []
```

Create minimal fixture files:
- `tests/fixtures/<name>/sample_output.sarif` — realistic minimal SARIF with 1-2 findings
- `tests/fixtures/<name>/empty_output.sarif` — valid SARIF with zero results

### Step 6 — Verify checklist

- [ ] `Dockerfiles/<name>/Dockerfile` — non-root, pinned base image, no secrets
- [ ] `Dockerfiles/<name>/analyze.sh` — `set -euo pipefail`, correct args, output verified
- [ ] `pipeline/config/analyzers.yaml` — entry added with correct `output_type`
- [ ] `resolve_scan_type()` updated if new output_type added
- [ ] Test file created with fixture files (no live paths)
- [ ] Run ruff on changed Python files

## How to trigger

```
/sast-analyzer-add name=my-tool image=sast-my-tool output_type=SARIF languages=python,go time_class=slow
```
