# Canonical deduplication

Canonical deduplication correlates findings from different analyzers when they
describe the same issue at the same source location in one product. It can link
a finding to an existing duplicate root, mark it as a review candidate, or
leave it unchanged.

![Canonical deduplication decision flow](assets/aist-canonical-dedupe.svg)

## Establish comparable findings

The first gate requires the same normalized file path and line. Findings that
do not provide both values are not eligible for canonical matching and follow
the configured fallback deduplication behavior.

Eligible findings are compared only within one product. Canonical matching
never correlates findings owned by different clients.

## Score the evidence

The score combines independent signals:

| Signal | Score |
|---|---:|
| Same non-zero CWE | +3 |
| Same canonical vulnerability family | +3 |
| Same normalized analyzer rule | +2 |
| Same component name or version | +1 |

Canonical families normalize common descriptions such as hardcoded secrets,
weak hashing, path traversal, cross-site scripting, command injection, and SQL
injection. This lets two tools use different titles without making the title
itself the durable identity.

## Apply the decision

The accumulated score is compared with two configured thresholds. A score at or
above the automatic threshold links the finding as a duplicate. A lower
positive score at or above the candidate threshold records a review candidate.
Candidate status alone does not change duplicate links unless candidate
application is explicitly enabled.

The thresholds allow deployments to choose between conservative automatic
linking and a larger human-review queue without changing the matching signals.

## Recompute existing findings

Operators can evaluate or apply the same decision to existing findings with the
[canonical deduplication recompute runbook](runbooks/canonical-deduplication-recompute.md).
