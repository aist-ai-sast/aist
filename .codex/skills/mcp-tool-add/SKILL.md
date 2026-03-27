---
name: mcp-tool-add
description: Add a new tool to the context extractor MCP server following established patterns for logging, path validation, isolated tests, and AI triage prompt integration.
---

# mcp-tool-add

Guided workflow for adding a new tool to the context extractor MCP server.
Ensures the tool follows all established patterns: logging decorator, path guard,
isolated test, and system prompt integration.

## Inputs

- `name` (required): tool function name in snake_case (e.g., `find_usages`)
- `description` (required): one-sentence description of what the tool does
- `branch` (required): `code` or `config` — which triage branch uses this tool

## Step-by-step workflow

### Step 1 — Read existing patterns

Before writing anything:
1. Read `sast-combinator/context_extractor_service/ansible/files/mcp_server.py`
   — find the `@log_tool` decorator definition and an existing simple tool as template.
2. Read the relevant module in `context_extractor/` that will contain the core logic
   (e.g., `extract.py`, `project_analysis.py`, `config_analysis.py`).
3. Check `aist/ai_triage_system_prompt.md` — understand where the new tool fits
   in the existing FLOW A (code) or FLOW B (config) sequence.

### Step 2 — Implement core logic in `context_extractor/`

Add the implementation in the appropriate module (NOT in `mcp_server.py`):

```python
# In context_extractor/<relevant_module>.py

def find_usages(source_dir: str, file_path: str, symbol: str) -> dict:
    """
    <description of what this function does internally>
    Returns: dict with keys: ...
    """
    # implementation here
```

**Rules:**
- All file access MUST validate `file_path` against `source_dir` (no `..`, no absolute paths).
  Copy the existing path validation pattern from another function in the same module.
- Return a plain dict or list — no MCP-specific types in this layer.
- Handle the case where the file does not exist or the symbol is not found gracefully
  (return empty result, not exception).

### Step 3 — Add MCP handler in `mcp_server.py`

Add the tool function after the last tool of the same category (code tools or config tools):

```python
@mcp.tool()
@log_tool
def find_usages(pipeline_id: str, file_path: str, symbol: str) -> str:
    """
    <description> — used by AI triage agents to <when to use it>.

    Args:
        pipeline_id: AIST pipeline identifier
        file_path: path relative to project root
        symbol: symbol name to search for
    Returns:
        JSON string with ...
    """
    source_dir = resolve_source_dir(pipeline_id)
    result = context_extractor.<module>.find_usages(source_dir, file_path, symbol)
    return json.dumps(result)
```

**Rules:**
- First argument is always `pipeline_id: str`.
- Call `resolve_source_dir(pipeline_id)` to get the project root — never use raw paths.
- Return `json.dumps(result)` — MCP tools return strings.
- Docstring must explain WHEN an AI triage agent should call this tool.
- Do NOT add error handling beyond what `@log_tool` already provides — it logs exceptions.

### Step 4 — Write isolated test

Add a test in `sast-combinator/context_extractor_service/ansible/files/tests/`.

**Choose the right test file:**
- Look for an existing thematic file that fits (e.g., `test_project_analysis.py`,
  `test_config_analysis.py`, `test_find_*.py`).
- If no suitable file exists, create `test_<module_name>.py`.

**Test structure:**
```python
import pytest
from context_extractor.<module> import find_usages

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "find_usages"

def test_find_usages_basic(tmp_path):
    """
    Scenario: agent triaging a finding at file_path:line calls find_usages
    to understand all call sites of a potentially dangerous function.
    Expected: returns list of locations where symbol appears.
    """
    # Arrange — create minimal realistic fixture (copy real code structure, anonymize values)
    source_file = FIXTURE_DIR / "example.py"
    # or use tmp_path for generated fixtures

    # Act
    result = find_usages(str(source_file.parent), "example.py", "dangerous_func")

    # Assert
    assert isinstance(result, list)
    assert any(item["line"] > 0 for item in result)

def test_find_usages_symbol_not_found(tmp_path):
    """Scenario: symbol does not exist — tool returns empty list, not exception."""
    (tmp_path / "empty.py").write_text("x = 1\n")
    result = find_usages(str(tmp_path), "empty.py", "nonexistent")
    assert result == []

def test_find_usages_path_traversal_blocked():
    """Security: path traversal attempt must be rejected."""
    with pytest.raises((ValueError, PermissionError)):
        find_usages("/some/root", "../../../etc/passwd", "root")
```

**Rules:**
- Fixture files must NOT reference live `/tmp/aist/projects/` paths.
- Test names must describe the real triage scenario, not the implementation.
- Cover: happy path, empty/not-found case, path traversal guard.

### Step 5 — Update system prompt

Open `aist/ai_triage_system_prompt.md` and add the new tool to the correct flow step:

```markdown
**Step N — <what the agent is doing>:**
```
find_usages(pipeline_id, file_path, "symbol_name")
```
Use when: <condition that tells the agent to call this tool>
Interpret result: <what to do with the output>
```

Place it in the logical sequence — after the step that produces the input this tool needs.

### Step 6 — Verify

- [ ] Core logic implemented in `context_extractor/` module
- [ ] MCP handler in `mcp_server.py` with `@log_tool` decorator
- [ ] `resolve_source_dir(pipeline_id)` used (no raw paths)
- [ ] Isolated test with fixture (no live paths)
- [ ] Path traversal test case present
- [ ] `ai_triage_system_prompt.md` updated
- [ ] Run ruff on changed files: `docker compose exec app ruff check <files>`

## How to trigger

```
/mcp-tool-add name=find_usages description="Find all usages of a symbol in the project" branch=code
```
