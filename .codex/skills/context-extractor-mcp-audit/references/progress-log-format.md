Store one JSON object per line in the progress log.

Recommended default path:

- `sast-combinator/context_extractor_service/ansible/files/tests/context_extractor_mcp_audit_progress.jsonl`

Required fields:

- `pipeline_id`
- `finding_id`
- `file_path`
- `line_number`
- `branch`
- `overall_status`
- `tool_statuses`
- `test_file`
- `fixture_paths`
- `note`

Allowed `overall_status` values:

- `covered_existing`
- `added_passing_test`
- `added_xfail_test`
- `skipped_duplicate`
- `needs_followup`

`tool_statuses` should be an object keyed by MCP tool name.

Allowed per-tool values:

- `applicable_pass`
- `applicable_fail`
- `applicable_exception`
- `not_applicable`

Example:

```json
{
  "pipeline_id": "69ec5b01",
  "finding_id": "12345",
  "file_path": "app/components/Example.tsx",
  "line_number": 27,
  "branch": "code",
  "overall_status": "added_xfail_test",
  "tool_statuses": {
    "classify_file": "applicable_pass",
    "extract_function": "applicable_pass",
    "find_imports": "applicable_pass",
    "find_decorators": "not_applicable",
    "find_identifiers": "applicable_pass",
    "trace_identifier_backward": "applicable_fail",
    "find_callers": "applicable_pass",
    "find_definition": "applicable_pass",
    "find_route_to_function": "applicable_pass"
  },
  "test_file": "sast-combinator/context_extractor_service/ansible/files/tests/test_real_finding_flow_validation_more.py",
  "fixture_paths": [
    "sast-combinator/context_extractor_service/ansible/files/tests/fixtures/real_finding_flow/example/Example.tsx"
  ],
  "note": "trace_identifier_backward loses the correct source assignment after live triage replay"
}
```
