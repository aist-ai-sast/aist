---
name: mcp-regression-fix
description: Fix failing or xfail-marked context extractor MCP regression tests by reproducing failures, identifying the root cause in context_extractor modules, and extending coverage when needed.
---

# mcp-regression-fix

Fix failing regression or marked as xfail tests for the MCP context extractor server.
The fix must address the root cause in `context_extractor/` modules — not special-case
the symptom in `mcp_server.py`. If existing test coverage is insufficient to prevent
the regression family from recurring, extend it.

## Inputs

- `test` (optional): specific test name or file to focus on. If omitted, run the full
  regression suite and fix all failures.

## Step 1 — Reproduce and understand the failure

Run the failing test(s) inside Docker:
```
docker compose --env-file .env.dev exec <service> \
  pytest sast-combinator/context_extractor_service/ansible/files/tests/<test_file>.py \
  -x -v 2>&1 | head -80
```

To run full regression suite use
```bash
./run-mcp-tests.zsh --clean
```

Read the full assertion error. Understand:
- **What the test asserts** — the expected behavior from a triage-agent perspective
- **What the code actually returns** — the wrong value or exception
- **Where in the call chain the divergence happens**:
  `mcp_server.<tool>()` → `context_extractor.<module>.<function>()` → Tree-sitter AST

Do NOT look at the fix yet. First write down in plain language:
> "The test expects X. The code returns Y. This means the parser/analyser does Z incorrectly."

## Step 2 — Trace the root cause to a specific module and function

The MCP server (`mcp_server.py`) is a thin dispatcher. Real logic lives in:

| Module | Handles |
|---|---|
| `context_extractor/extract.py` | Function/method extraction, `code_on_line`, AST node boundaries |
| `context_extractor/project_analysis.py` | `find_callers`, `trace_identifier_backward`, `find_definition`, `find_route_to_function`, `find_imports`, `find_decorators` |
| `context_extractor/identifiers.py` | `find_identifiers` — reads/writes on a line |
| `context_extractor/config_analysis.py` | Config block extraction, env vars, overrides |
| `context_extractor/ts_utils.py` | Language detection, parser creation, node type helpers |
| `context_extractor/compress.py` | Large AST node compression |

Trace the call: read the relevant function in the module. Find the exact line or logic
where the wrong value is produced.

**Common root cause categories:**

| Category | Where to look | Signature |
|---|---|---|
| Wrong language detected | `ts_utils.py` — `detect_language()` | `.tsx`, `.mjs`, `.vue` extension missing or wrong |
| Node type not handled | `ts_utils.py` or the calling module | `if node.type == "..."` list incomplete |
| Assignment pattern not recognised | `identifiers.py` | Go `:=`, destructuring, template literals |
| Function boundary wrong | `extract.py` | Wrong start/end line for a language construct |
| Traversal stops too early | `project_analysis.py` | Hop limit, missing node type in walk |
| Config block boundary wrong | `config_analysis.py` | YAML/HCL/TOML nesting not handled |

Name the category before writing code. This determines where and how to fix.

## Step 3 — Identify the problem family

Before fixing, answer:
> "What is the general class of inputs that would trigger this same bug?"

Examples:
- "Any TypeScript file using template literals in an assignment"
- "Any Go file with short variable declaration (`:=`) as the sink line"
- "Any `.tsx` file — because the extension wasn't in the language map"
- "Any YAML config block that uses multi-line anchors"

The fix must handle **all members of the family**, not just the specific example in the failing test.

To find other members: look for similar patterns in existing test fixtures and in the
live finding corpus if available. Check the progress log in
`context-extractor-mcp-audit/` for findings with `applicable_fail` on the same tool.

## Step 4 — Write the fix in `context_extractor/`

Fix in the module identified in Step 2. Rules:

**DO:**
- Fix the parsing or traversal logic to handle the full family
- Add or extend node type lists / extension maps to cover all variants
- Handle the new case with the same code path as similar existing cases (no special-case branches)
- Keep functions single-responsibility

**DO NOT:**
- Add `if file_path.endswith(".tsx")` workarounds in `mcp_server.py` handlers
- Return a hardcoded value to make a specific test pass
- Copy-paste a function with a minor tweak — extend the existing one
- Change function signatures unless necessary (callers in `mcp_server.py` will break)

After writing the fix, manually trace through the failing test scenario in your head
to verify the fix produces the expected result.

## Step 5 — Assess and extend test coverage

After fixing, ask:
> "Does the existing test suite cover the full problem family I identified in Step 3?"

Check the relevant test files:
- `test_mcp_server_regressions.py` — MCP-level integration (monkeypatches `_read_source`)
- `test_extract_regressions.py` — extraction unit tests
- `test_navigation_regressions.py` — traversal/callers/definitions
- `test_flow_break_regressions.py` — data flow tracing
- `test_project_and_config_regressions.py` — config analysis
- `test_security_edge_cases.py` — security-relevant edge cases

**Add tests if any of these are true:**
- Another member of the family (different language, different syntax pattern) is not covered
- The boundary condition that caused the bug is not explicitly tested
- The empty/not-found case for the new family member is not tested

**Test authoring rules:**
- Use the `_stub_read_source` monkeypatch pattern for MCP-level tests:
  ```python
  def _stub_read_source(source: str, file_name: str):
      def _reader(_pipeline_id, _file_path):
          return source, Path(file_name)
      return _reader

  def test_tool_handles_<new_case>(monkeypatch):
      """
      Scenario: <describe the real triage scenario that would hit this path>
      Previously this caused <wrong result / exception>.
      """
      source = """\
  <minimal realistic source code that triggers the case>
  """
      monkeypatch.setattr(mcp_server, "_read_source",
                          _stub_read_source(source, "example.<ext>"))
      result = mcp_server.<tool>("pipe", "example.<ext>", <line>)
      assert <correct assertion>
  ```
- For unit tests on `context_extractor/` functions directly, no monkeypatch needed —
  pass source string directly to the function.
- Test name format: `test_<tool>_should_<expected_behaviour>_for_<language_or_construct>`
- Docstring must describe the real triage scenario (agent perspective), not the implementation.
- Inline source fixtures only (no files). If fixture is >40 lines, extract to
  `tests/fixtures/<tool_name>/` directory.
- Do NOT use live `/tmp/aist/projects/` paths.

**For confirmed bugs that are not yet fixed** (e.g., discovered during audit but deferred):
use `pytest.mark.xfail(strict=True)` with reason containing:
`pipeline_id | file_path:line | tool | short description`

## Step 6 — Verify

Run all tests (including xfail) to see the full picture:
```bash
./run-mcp-tests.zsh --clean
```
This runs all tests and shows which xfail tests now xpass (line: `XPASS`).

For previously xfail tests that now xpass: remove their `@pytest.mark.xfail` decorator.

To run a specific test file directly against the already-built image (faster, no rebuild):
```bash
docker run --rm -w /app aist-context-extractor-mcp:test \
  python -m pytest tests/<test_file>.py -v
```

**Done when:**
- [ ] All originally failing tests pass
- [ ] xfail tests that now pass have their decorator removed
- [ ] No new test failures introduced
- [ ] Fix covers the full problem family (not just the specific failing case)
- [ ] New tests added for uncovered family members (if any)
- [ ] ruff passes on changed files

## How to trigger

```
/mcp-regression-fix
/mcp-regression-fix test=test_mcp_server_regressions.py::test_find_identifiers_should_support_tsx_files
```
