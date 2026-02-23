# AIST Canonical Dedupe

## Strategy
- Keep UI-friendly titles, but make dedupe inputs stable.
- Use cross-scanner canonical matching with a strict hard gate: same normalized `file_path` and `line`.
- Auto-mark duplicates only on strong confidence (`score >= 5`), tag medium confidence as candidates (`score 3-4`).

## Family Mapping
| Family | Typical Patterns | CWE fallback |
| --- | --- | --- |
| `private_key` | `private key`, `rsa key` | `321` |
| `aws_key` | `aws key`, `access key`, `AKIA...` | `798` |
| `hardcoded_secret` | `hardcoded secret/password/token` | `798` |
| `ssl_verification` | `ssl verify`, `no verify`, `tls verify` | `295` |
| `weak_hash` | `md5`, `sha1`, `weak hash` | `327` |
| `path_traversal` | `path traversal`, `directory traversal` | `22` |
| `open_redirect` | `open redirect`, `javascript/OR` | `601` |
| `xss_dom` | `xss`, `dom xss`, `cross site scripting` | `79` |
| `eval_dynamic_code` | `eval`, `dynamic code` | `95` |
| `command_injection` | `command injection`, `os command` | `78` |
| `sql_injection` | `sql injection`, `sqli` | `89` |
| `postmessage_origin` | `postmessage`, `origin check` | `346` |

## Scoring
- `+3` same non-zero CWE
- `+3` same canonical family
- `+2` same normalized rule
- `+1` same component name or version

Decision:
- `>= 4`: duplicate
- `2..3`: candidate tag only
- `< 2`: no match

## Runtime Dedupe Config
- Algorithm for Snyk/Semgrep/Horusec/Bearer: `unique_id_from_tool_or_hash_code`
- Hash fields: `vuln_id_from_tool`, `file_path`, `line`, `cwe`
- `Horusec Scan`: allows null CWE

## Management Command
- Command: `recompute_aist_duplicates`
- Modes:
  - `--dry-run`
  - `--apply`
- Filters:
  - `--pipeline-id`
  - `--product-id`
  - `--since YYYY-MM-DD`
  - `--limit`
  - `--clear-existing-aist-duplicate-tags`

### Examples
```bash
python3 manage.py recompute_aist_duplicates --dry-run --product-id 12 --since 2026-01-01
```

```bash
python3 manage.py recompute_aist_duplicates --apply --pipeline-id 5ae48a36
```

## Limitations
- Hard gate requires both path and line; findings without these fields are skipped from canonical matching.
- Candidate tagging is conservative and does not mutate duplicate links.
