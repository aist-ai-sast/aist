# AI triage execution

AI triage begins after imported findings are ready for review. AIST first
selects a set of findings, then sends that same set through the configured
execution backend. Selection and backend choice are independent decisions.

![AI triage execution flow](../assets/ai-triage-execution.svg)

## Select findings

Automatic selection applies the rules captured when the pipeline was launched.
Manual selection lets an authorized reviewer choose findings from the pipeline.
Findings that already contain an analyzer-produced AI verdict are excluded from
post-import automatic selection.

If no findings remain after selection, the pipeline completes without creating
an AI request.

## Execute through the selected backend

Webhook mode sends project context, the selected finding identifiers, the
pipeline identity, and a callback address to the configured n8n webhook. The
accepted callback persists the resulting AI responses. A failed request leaves
the pipeline with a visible degraded outcome.

Local mode resolves the project's Claude integration and source path, then asks
the local AI bridge to start an isolated CLI operation through the shared Unix
socket. Missing credentials or a rejected bridge request becomes a visible
degraded outcome with diagnostic context rather than an unauthenticated
fallback.

## Persist the verdict

Each response is associated with the originating pipeline and finding. A false
positive verdict closes and marks the finding false positive in the normal
callback flow. True positive and uncertain findings remain active for human
review.

The AI response does not remove reviewer authority. An authorized reviewer can
later change the finding disposition according to the normal review workflow.
See [AI triage](../product/ai-triage.md) and
[finding review](../product/finding-review.md).
