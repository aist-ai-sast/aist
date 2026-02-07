# Client UI UX Skeleton

This document captures the agreed UX structure for the client-facing UI.

## Information Architecture
- Products
- Findings
- Finding Detail (dedicated page)
- Pipelines
- Search
- Settings

## Global Shell
- Left sidebar (collapsible): Products, Findings, Pipelines, Search, Settings
- Top bar: global search + user menu + product scope selector (optional)

## Products
**Goal:** quickly identify risk hotspots and manage access (if role allows).

**Layout**
- Header: title + total count + last sync time
- Control bar: search, filters (status, risk state, tags), CTA `Manage access`
- Card grid:
  - Name
  - Severity mini-bar (Critical → Info)
  - Active findings count
  - Risk state badges (Risk Accepted / Under Review / Mitigated)
  - Last pipeline status + time
  - CTAs: `View findings`, `View pipelines`

**States**
- Loading: skeleton cards
- Empty: no products
- Error: inline error + retry

## Findings (Triage Workspace)
**Goal:** fast triage with AI context and code snippet viewer.

**Layout**
- Header: title + active filter chips
- Left filter panel (sticky)
  - Product (multi)
  - Severity (Critical/High/Medium/Low/Info)
  - Status (Enabled/Disabled)
  - Risk state (Risk Accepted / Under Review / Mitigated)
  - AI verdict (TP / FP / Uncertain)
  - Date range
  - Tool
  - Has snippet / Has AI comment
- Findings stream (center)
  - Virtualized cards
  - Severity pill
  - Title + status
  - File + line
  - AI verdict badge
  - Snippet preview (2–4 lines)
  - Quick actions: Enable/Disable, Comment, Export (role-based)
- Detail panel (right)
  - Summary (title, product, severity, status, risk state)
  - AI section (reasoning + scores + refs)
  - Code viewer (file + line highlight + copy)
  - Actions (role-based)

**States**
- Loading: skeleton list + panel
- Empty: no findings match filters
- Error: inline error + retry
- No snippet: placeholder in code section

## Finding Detail (Dedicated Page)
**Goal:** shareable deep link with full context.

**Layout**
- Header: title, severity, status, product, risk state
- AI summary section
- Code viewer (full width, line highlighting)
- Metadata (tool, rule, EPSS, exploitability, impact)
- History/notes
- Actions: status change, comment, export (role-based)

## Pipelines
**Goal:** status tracking and AI result navigation.

**Layout**
- Header: title + product selector
- Control bar: status filter, date range, branch/commit search
- Timeline cards:
  - Status badge
  - Started/Finished
  - Branch/Commit
  - Issues found
  - Actions performed (AISTLaunchConfigAction)
  - CTA: `Open AI results`
- Detail drawer (on select)
  - AI summary (TP / FP / Uncertain)
  - Warnings + progress
  - Export

**States**
- Loading: skeleton cards
- Empty: no pipelines yet
- Error: inline error + retry

## Search
**Goal:** fast lookup across products/findings/pipelines.

**Layout**
- Global search input
- Tabs: Findings / Products / Pipelines
- Card results with deep-links

## Permissions
- All actions are gated by the user’s DefectDojo role for the product.
- Findings status uses `active` (Enabled/Disabled).
- Risk state derived from `risk_accepted`, `under_review`, `is_mitigated`.

## Integration Notes
- Finding detail uses `originalFinding.id == finding.pk` to join AI response.
- Code snippet from `/projects_version/<id>/files/blob/<path>`.
