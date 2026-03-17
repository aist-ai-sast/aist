# AIST Canonical Deduplication

Cross-scanner deduplication for SAST findings. Standard DefectDojo deduplication
relies on hash codes or unique tool IDs, which are scanner-specific and do not
work across scanners. This module identifies the same vulnerability reported at the
same location by different scanners — without requiring them to agree on rule names
or CWE numbers.

## Supported scan types

| Scanner | Value in `SUPPORTED_SCAN_TYPES` |
|---|---|
| Snyk Code | `Snyk Code Scan` |
| Semgrep | `Semgrep JSON Report` |
| Horusec | `Horusec Scan` |
| Bearer | `Bearer CLI` |

Findings from any other scan type bypass canonical deduplication and fall back to
the standard DefectDojo algorithm (see [Fallback](#fallback-deduplication)).

---

## How it works

### Phase 1 — Scope resolution

When a new finding arrives, the algorithm fetches all existing findings from the
same **product** that share the same **file path** and **line number** (across all
supported scanners). This is the *comparison scope*.

```
New finding:
  scanner=Snyk Code, file=src/net.py, line=10, title="SSL verify False"

Scope fetched from DB:
  - id=1  scanner=Semgrep,   file=src/net.py, line=10, title="SSL no-verify"
  - id=2  scanner=Horusec,   file=src/net.py, line=10, title="TLS disabled"
  - id=3  scanner=Snyk Code, file=src/net.py, line=10, title="SSL verify False"  ← new
```

Findings without a `line` or `file_path` are ineligible for canonical matching and
are routed to [fallback](#fallback-deduplication) immediately.

---

### Phase 2 — Signature extraction

Each finding is reduced to a scanner-agnostic `CanonicalSignature`:

| Field | How it is derived |
|---|---|
| `normalized_file_path` | lower-cased, backslashes→forward slashes, double slashes collapsed |
| `line` | integer line number |
| `family` | inferred from `vuln_id_from_tool` + `title` via regex patterns (see table below) |
| `cwe` | explicit CWE from the finding, or the family's canonical CWE if missing |
| `cwe_inferred` | `True` when CWE was filled in from the family, not from the finding itself |
| `normalized_rule` | `vuln_id_from_tool` (or `title`) lower-cased, non-alphanumeric → `_`, with cross-scanner aliases applied |
| `component_name` | lower-cased component name |
| `component_version` | lower-cased component version |

#### Vulnerability families

| Family | Matched patterns |
|---|---|
| `PRIVATE_KEY` | `private_key`, `rsa_key` |
| `AWS_KEY` | `aws_key`, `aws_secret`, `AKIA…` |
| `HARDCODED_SECRET` | `hardcoded_secret/password/token`, `detected_secret`, `detected_jwt_token` |
| `SSL_VERIFICATION` | `ssl_verify`, `no_verify`, `tls_verify` |
| `WEAK_HASH` | `md5`, `sha1`, `weak_hash` |
| `PATH_TRAVERSAL` | `path_traversal`, `directory_traversal` |
| `OPEN_REDIRECT` | `open_redirect`, `javascript/or` |
| `XSS_DOM` | `xss`, `cross_site_scripting`, `dom_xss` |
| `EVAL_DYNAMIC_CODE` | `eval`, `dynamic_code` |
| `COMMAND_INJECTION` | `command_injection`, `os_command` |
| `SQL_INJECTION` | `sql_injection`, `sqli` |
| `POSTMESSAGE_ORIGIN` | `postmessage`, `origin_check` |
| `UNKNOWN` | anything else |

---

### Phase 3 — Scoring

Findings are sorted within the group by `(created, id)`. Each finding is scored
against every predecessor. The score is the sum of matched evidence:

| Evidence | Points | Condition |
|---|---|---|
| **CWE explicit match** | +3 | Both findings have a real CWE (not inferred) and they are equal |
| **CWE mixed match** | +2 | CWEs are equal but exactly one is inferred from the family |
| **Family match** | +3 | Both share the same non-`UNKNOWN` family |
| **Rule key match** | +2 | Normalized rule keys are equal (after cross-scanner aliasing) |
| **Component match** | +1 | `component_name` or `component_version` is the same |

**Anti-spam guard:** if the only positive evidence is a family match where *both*
families are inferred (no explicit CWE, no rule match, no component match), the
score is zeroed out and the verdict is `NO_MATCH`. This prevents weak family
signals from polluting the candidate list.

#### Score → verdict

| Score | Verdict | Meaning |
|---|---|---|
| ≥ `AUTO_THRESHOLD` | **DUPLICATE** | High confidence — same vulnerability, different scanner |
| ≥ `CANDIDATE_MIN` and < `AUTO_THRESHOLD` | **CANDIDATE** | Possible duplicate — human review recommended |
| < `CANDIDATE_MIN` | **NO_MATCH** | Not related |

Default code values: `AUTO_THRESHOLD = 4`, `CANDIDATE_MIN = 2`.
Production values (from `aist_site/settings.py`): `AUTO_THRESHOLD = 2`, `CANDIDATE_MIN = 1`.

---

### Phase 4 — Apply

| Verdict | What happens |
|---|---|
| **DUPLICATE** | `set_duplicate(finding, root)` is called. Finding gets tag `aist:duplicate:auto`. |
| **CANDIDATE** | Finding gets tag `aist:duplicate:candidate`. No duplicate link is set. |
| **NO_MATCH** | Nothing changes. |

`root` is the **oldest** finding in the group that scored best. If the best
previous finding is itself already a duplicate, its `duplicate_finding` is used as
root (chain resolution).

---

### Fallback deduplication

Two categories of findings are handled by the standard DefectDojo algorithm:

1. **Unsupported scan type** — the entire finding is routed to the default algorithm
   (`hash_code`, `unique_id_from_tool`, or `legacy`) based on the test's
   `deduplication_algorithm` setting.

2. **Eligible scan type but no `line` / `file_path`** (`fallback_ineligible=True`)
   — canonical matching cannot run without a location, so the standard algorithm is
   applied as a best-effort fallback.

---

## Scoring examples

### Example 1 — DUPLICATE (score 6, production threshold 2)

```
Semgrep finding:
  vuln_id = "python.lang.security.audit.ssl-no-verify"
  title   = "SSL verification disabled"
  file    = "src/net.py", line = 10, cwe = 295

Snyk Code finding (new):
  vuln_id = "python/SSLVerificationBypassed"
  title   = "SSL verify False"
  file    = "src/net.py", line = 10, cwe = 295
```

| Evidence | Points |
|---|---|
| CWE 295 == 295, both explicit | +3 |
| Family SSL_VERIFICATION == SSL_VERIFICATION | +3 |
| Rule keys differ (`python_lang_security_audit_ssl_no_verify` vs `python_ssl_verification_bypassed`) | +0 |
| **Total** | **6** |

Verdict: **DUPLICATE** (≥ 2 in production).

---

### Example 2 — DUPLICATE via cross-scanner rule alias (score 4)

Semgrep and Snyk Code use different rule IDs for JWT / non-crypto hardcoded
secrets. The alias table maps both to `secret_jwt_or_noncrypto_hardcoded`.

```
Semgrep finding:
  vuln_id = "generic_secrets_security_detected_jwt_token_detected_jwt_token"
  file    = "src/config.ts", line = 122, cwe = 321

Snyk Code finding (new):
  vuln_id = "javascript_hardcodednoncryptosecret"
  file    = "src/config.ts", line = 122, cwe = 547
```

| Evidence | Points |
|---|---|
| CWE 321 ≠ 547 | +0 |
| Family HARDCODED_SECRET == HARDCODED_SECRET | +3 |
| Rule key alias `secret_jwt_or_noncrypto_hardcoded` == same | +2 |
| **Total** | **5 → capped to score, verdict DUPLICATE** |

Verdict: **DUPLICATE**.

---

### Example 3 — CANDIDATE (score 1, production threshold 1)

```
Finding A:
  vuln_id = "custom_rule_x", file = "app/views.py", line = 88, cwe = None
  component_name = "lodash"

Finding B (new):
  vuln_id = "another_rule",  file = "app/views.py", line = 88, cwe = None
  component_name = "lodash"
```

| Evidence | Points |
|---|---|
| No CWE on either side | +0 |
| Family UNKNOWN (no pattern matched) | +0 |
| Rule keys differ | +0 |
| component_name "lodash" == "lodash" | +1 |
| Anti-spam guard: family is UNKNOWN, not inferred-family-only | — |
| **Total** | **1** |

Verdict: **CANDIDATE** (score ≥ 1, < 2). Finding gets tag `aist:duplicate:candidate`.

---

### Example 4 — NO_MATCH blocked by anti-spam guard

```
Finding A:
  vuln_id = "path_traversal_rule", file = "api/upload.py", line = 33, cwe = None

Finding B (new):
  vuln_id = "another_traversal_rule", file = "api/upload.py", line = 33, cwe = None
```

Both map to family `PATH_TRAVERSAL` (family_match → +3), but:
- CWE is `None` on both → both use inferred CWE 22
- No explicit CWE → `cwe_inferred = True` for both
- Rule keys differ → +0
- No component → +0

Anti-spam guard fires: family match with both sides inferred, no other evidence.
Score reset to 0. Verdict: **NO_MATCH**.

---

## Configuration

Two environment variables control the scoring thresholds:

### `DD_AIST_CANONICAL_AUTO_DUPLICATE_THRESHOLD`

Minimum score to automatically mark a finding as a duplicate.

| Value | Effect |
|---|---|
| **2** (production default) | Any two pieces of matching evidence trigger auto-dedup. Catches most cross-scanner pairs. Risk: slightly higher false-positive rate for findings with similar-but-unrelated titles. |
| **4** (code default) | Requires strong corroboration (e.g. explicit CWE + family). More conservative. Suitable if you prefer manual review over automation. |
| **6** | Only triggers when CWE, family, and rule all match. Very strict — useful if scanners in your stack use inconsistent CWE assignments. |

### `DD_AIST_CANONICAL_CANDIDATE_MIN_SCORE`

Minimum score to tag a finding as a candidate for human review.
Must be less than `AUTO_THRESHOLD` (enforced automatically).

| Value | Effect |
|---|---|
| **1** (production default) | A single component match is enough to surface a candidate. Maximises recall — you'll see more potential duplicates, including weak ones. |
| **2** (code default) | Requires at least two weak signals (e.g. rule key match). Reduces noise in the candidate list. |
| **0** | Disable the candidate zone entirely — every sub-threshold finding is treated as NO_MATCH. |

### Interaction between the two settings

```
CANDIDATE_MIN = 1, AUTO_THRESHOLD = 2

Score 0 → NO_MATCH
Score 1 → CANDIDATE   (tagged, not linked)
Score 2+ → DUPLICATE  (linked via set_duplicate)
```

```
CANDIDATE_MIN = 2, AUTO_THRESHOLD = 4

Score 0–1 → NO_MATCH
Score 2–3 → CANDIDATE
Score 4+  → DUPLICATE
```

**Safety rule:** if `CANDIDATE_MIN >= AUTO_THRESHOLD`, the code automatically
lowers `CANDIDATE_MIN` to `AUTO_THRESHOLD - 1` to keep the zones non-overlapping.

---

## Promoting candidates

The `recompute_aist_duplicates` management command can promote existing candidates
to confirmed duplicates:

```bash
python manage.py recompute_aist_duplicates --apply --apply-candidates
```

| Flag | Behaviour |
|---|---|
| `--dry-run` | Compute decisions and print groups, make no DB changes |
| `--apply` | Apply DUPLICATE verdicts (call `set_duplicate`) |
| `--apply-candidates` | Also promote CANDIDATE findings to duplicates |
| `--apply-candidates` without `--apply` | `--apply` is implied automatically |
| `--clear-existing-aist-duplicate-tags` | Strip all `aist:duplicate:*` tags before recompute |
| `--product-id N` | Restrict to one product |
| `--since YYYY-MM-DD` | Only findings created on or after this date |
| `--limit N` | Cap total findings processed |

---

## Tags

| Tag | Meaning |
|---|---|
| `aist:duplicate:auto` | Automatically confirmed duplicate (set_duplicate was called) |
| `aist:duplicate:candidate` | Possible duplicate — pending human review |
