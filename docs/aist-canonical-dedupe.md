# Canonical deduplication

Canonical deduplication correlates findings when they describe the same issue at
the same source or web location. Static findings are scoped to one product; DAST
findings are scoped to `AISTPipeline.dast_binding` on the pipeline that owns their Test, so
two DAST targets attached to one product cannot collide. Binding is pipeline execution
context, not part of a Finding or its canonical signature. The findings commonly come from
different analyzers, but analyzer identity is not a matching requirement. It can link
a finding to an existing duplicate root, mark it as a review candidate, or
leave it unchanged.

For every supported AIST import, DefectDojo's existing exact-identity match runs
first. It uses the producer-owned `unique_id_from_tool` to make repeated imports
idempotent. SAST retains DefectDojo's product/engagement and test-type scope;
DAST additionally requires the same `DastProjectBinding`. Canonical correlation
processes only findings that remain after exact identity matching. The safety and
application rules for both stages are defined below.

![Canonical deduplication decision flow](assets/aist-canonical-dedupe.svg)

## Establish comparable findings

For static findings, the first gate requires the same normalized file path and
line. For DAST findings it uses the canonical endpoint representation already
owned by DefectDojo (`get_endpoints_as_url`), rather than maintaining a second URL
normalizer in AIST. Similar endpoint, CVE, component, service, parameter, family,
and title evidence can place two findings in the review-candidate set, but cannot
authorize an automatic merge.

Automatic DAST matching uses exact typed identities instead:

- a route identity is the complete set of affected endpoint shapes plus explicit
  CWE, component, service, and parameter. A concrete path omits the deployment
  host from its shape, while a root endpoint retains its host;
- an external identity is the exact CVE set plus component name and version.

An identity that describes more than one Finding inside a single Test is not
unique enough for automatic matching. If different exact identities point to
different roots, the result is also a review candidate rather than a merge.
Consequently partial overlap cannot create a transitive `A -> B -> C` cluster.

## Score the evidence

The score combines independent signals:

| Signal | Score |
|---|---:|
| Same explicit non-zero CWE | +3 |
| Same CWE when exactly one static finding inferred it from the family | +2 |
| Same canonical vulnerability family | +3 |
| Same normalized analyzer rule | +2 |
| Same component name | +1 |
| Strong normalized title-token overlap | +1 |
| Same explicit vulnerability ID (DAST) | +3 |
| Same structured parameter or service (DAST) | +1 each |

Title overlap requires at least three shared content tokens and a Jaccard score
of at least 0.4 after generic security words are removed. Canonical families
normalize common descriptions such as hardcoded secrets,
weak hashing, path traversal, cross-site scripting, command injection, and SQL
injection. This lets two tools use different titles without making the title
itself the durable identity.

For DAST, score never authorizes automatic linking. It only ranks candidates.
Automatic linking requires equality of one complete identity after the binding
scope and cluster-ambiguity checks. Severity is reported independently and never
participates in identity. Applying an exact match has a separate safety gate: its
root must still be actionable and at least as severe as the new finding. A root
that was closed, accepted, marked false positive/out of scope, or has lower
severity leaves the new finding active as a review candidate.

DAST CWE and CVE values are read only from their imported structured fields. The
canonical family classifier never turns a DAST title into a synthetic CWE.

The DAST `vuln_id_from_tool` is an occurrence/re-import identifier, not an analyzer
rule, and therefore never contributes rule evidence. Its corresponding
`unique_id_from_tool` carries producer occurrence identity. Explicit CVE-style
vulnerability IDs remain independent semantic evidence.

## Apply the decision

For static findings, the accumulated score is compared with the configured
automatic and candidate thresholds. For DAST, a score at or above the candidate
threshold records a review candidate, while only an exact identity cluster can
link a duplicate automatically. Explicit candidate application may promote a
canonical candidate; it never promotes an exact-identity candidate held by the
safety gate.

Each applied pipeline batch writes a summary and every automatic/candidate pair
to the existing AIST pipeline log before DefectDojo's duplicate cleanup can remove
the duplicate row. The diagnostic contains Finding/Test/root identifiers, bounded
titles, identity kind, score, and decision reason; it does not log endpoint or
payload contents.

## Recompute existing findings

Operators can evaluate or apply the same decision to existing findings with the
[canonical deduplication recompute runbook](runbooks/canonical-deduplication-recompute.md).
