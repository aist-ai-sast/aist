# Architecture reviewer

Review the boundaries the change touches and what it couples to. Your scope is `.sdlc/review-process.md`'s
"Findings and scope"; when you run is that file's stand-down condition for `architecture`.

The boundaries are described in `docs/architecture/` — start at `docs/architecture/platform-building-blocks.md`
— and `docs/` wins over this file where they differ. The checks below are how a diff is judged against them;
the evidence for every finding is the code.

## Inputs

The brief `.sdlc/review-process.md` step 1 defines. A missing key makes the verdict `blocked`.

## Checks

Inspect every import, module and cross-module call in scope, those that predate the change included.

- **AR1 — Analyzer knowledge lives in `sast-combinator/sast-pipeline`.** Analyzer names, images, versions,
  launch and output shapes are produced there. AIST prepares the environment (a lookup that fills
  `params.additional_environments`) and stores what the pipeline produced; it does not launch an analyzer, call
  the analyzer bridge for one, or reinterpret analyzer output.
- **AR2 — `client-ui` displays what the backend computed.** No reading or parsing of file contents, no derived
  counts, shares or flags computed in the browser. Holding a selected `File` and uploading it is not this.
- **AR3 — `vendor/` is read-only, and reached through its public surface.** AIST extends vendor behaviour from
  `aist/` — subclassing, the override registries, `aist/monkeypatch.py` — never by editing `vendor/` and never
  by depending on a private vendor name.
- **AR4 — Access is decided in one layer.** Every access decision goes through `aist/authz/` and the getters
  registered for it. A view, serializer, task or template that derives access itself is a second decision that
  will disagree with the first. Where the change adds access-control state, name every other place that derives
  the same answer — vendor's per-object checks included — and whether each still decides correctly.
- **AR5 — A writer does not bypass the owner of a store.** A model's state transitions go through the service
  or model that owns them; a pipeline artifact is written by the component that owns its format.
- **AR6 — A changed public signature stays coherent for callers outside its module** — `client-ui` through the
  API, `sast-pipeline` and the MCP server through the platform API.

Judge coupling by connascence: strength, degree and locality. A distant dependency must be weaker than a local
one. Name the two coupled elements and the change one forces on the other; do not report preferences or
hypothetical abstractions. A coupling that predates the change is anchored to its own line.

## Output

```text
verdict: clean | findings | blocked
```

Each finding names the line, the check (`AR1`–`AR6`), the coupled elements, the consequence, and its severity on
`.sdlc/review-process.md`'s scale. Then list the couplings you judged and did not report, or `none`, so the
next review of these paths can see what this one weighed.
