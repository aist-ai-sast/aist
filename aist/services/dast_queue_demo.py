"""
A demo snapshot of a finished run's work queue.

A development install has no DAST gateway, and the route to one is not something a laptop can
stand up: the policy that guards it refuses loopback outright and admits a private address only
behind a trusted VPN route. So the demo gets what a worker would otherwise have stored -- a
snapshot -- and the page renders it exactly as it renders a real one, without knowing who wrote it.

The payload is synthetic and shaped like the published worked examples
(`contracts/examples/queue/*.json` in the DAST repository). It carries no real host, run id or
verdict: everything here is invented for the demo organisations this installation bootstraps.
"""

from __future__ import annotations

from typing import Any


def _field(name: str, **overrides: Any) -> dict[str, Any]:
    field = {
        "name": name, "type": "text", "required": False, "read_only": False,
        "max_length": 4000, "value": "", "values_from": "", "help": "",
    }
    field.update(overrides)
    return field


def _vocabulary() -> dict[str, list[dict[str, Any]]]:
    def entry(identifier: str, label: str, help_text: str, icon: str, severity: str = "", group: str = "") -> dict:
        return {"id": identifier, "label": label, "help": help_text, "icon": icon,
                "severity": severity, "group": group}

    return {
        "status": [
            entry("pending", "Pending", "Queued and unclaimed. The row's wait reason says what for.", "clock", group="open"),
            entry("running", "Running", "Handed out to a session that holds its claim.", "play", group="open"),
            entry("completed", "Completed", "Finished and published its result.", "check", group="settled"),
            entry("failed", "Failed", "Exhausted its retries.", "x", "bad", "open"),
            entry("cancelled", "Cancelled", "An operator dropped it; it asserts no coverage.", "ban", "attention", "settled"),
            entry("delayed", "Delayed", "Offered to a later run; it asserts no coverage until one takes it.", "forward", "attention", "settled"),
            entry("admission-rejected", "Not admitted", "A lead the agent raised that the run did not take as work.", "bulb", group="open"),
            entry("active", "Holding", "An operator pause in force.", "pause", "attention", "open"),
        ],
        "wait": [
            entry("session-capacity", "No free session", "The run's model-session ceiling is reached.", "alert", "attention"),
            entry("research-environment", "Research slot taken", "Another investigating job holds the run's one research reservation.", "alert", "attention"),
            entry("retry-backoff", "Retrying", "Its next attempt is not due yet.", "clock"),
            entry("cancel", "Cancel requested", "It settles once its executor confirms.", "ban", "attention"),
        ],
        "kind": [
            entry("job", "Job", "Work the run has declared and may hand out.", "play"),
            entry("planned", "Planned", "Work the run owes but has not created yet.", "clock"),
            entry("hold", "Pause", "An operator pause on the whole queue.", "pause"),
            entry("proposal", "Lead", "A lead the agent raised that the run did not admit.", "bulb"),
        ],
        "phase": [
            entry("0", "0", "spec & environment", ""),
            entry("6", "6", "depth: floor, explore, discovery", ""),
            entry("7", "7", "verify", ""),
            entry("8", "8", "audit, report, durability", ""),
        ],
        "run_state": [
            entry("running", "Running", "Handing out work and taking its results.", "play", group="open"),
            entry("succeeded", "Succeeded", "Finished. The queue is the record of what it covered.", "check", group="settled"),
        ],
        "contract": [
            entry("check", "Check", "Settles one question against the target.", "play"),
            entry("verify", "Verification", "Independently re-tests a verdict this run recorded.", "check"),
            entry("report", "Report", "Writes up a confirmed finding.", "note"),
        ],
        "subject_role": [
            entry("assigned", "Will answer", "A registered result this work owes and has not answered yet.", "clock"),
            entry("outcome", "Answered", "A verdict this work recorded.", "check"),
        ],
        "verdict_status": [
            entry("vuln", "Vulnerable", "The check found the defect it was looking for.", "alert", "bad"),
            entry("safe", "Safe", "The check looked and the defect is not there.", "check"),
        ],
    }


def demo_descriptor(*, digest: str, target_host: str) -> dict[str, Any]:
    """The descriptor a finished demo run would have published."""
    return {
        "contract_version": "2.0",
        "descriptor_digest": digest,
        "run_state": "succeeded",
        "capabilities": ["queue:read", "queue:control", "run:annotate"],
        "client_hints": {"poll_interval_seconds": 60, "max_items_per_page": 500},
        "mutation_envelope": {
            "fields": [
                _field("request_id", required=True, max_length=128,
                       help="Minted by the caller. An identical retry returns the first receipt."),
                _field("expected_revision", type="int", required=True, max_length=0,
                       help="The revision of the queue page this command was decided against."),
            ],
        },
        "execution": {
            "claim_statuses": ["running", "waiting", "publication_pending"],
            "parallel_contracts": ["report", "repair"],
            "serial_note": "One investigating job runs at a time: two at once make either one's evidence impossible to attribute.",
            "parallel_note": "Work that only reads what the run already recorded runs alongside it.",
            "session_capacity": 1,
        },
        "vocabulary": _vocabulary(),
        "enums": {
            "scope": [{"id": target_host, "title": target_host}],
            "skill": [{"id": name, "title": name} for name in ("idor", "sqli", "xss", "session-management")],
        },
        "run_actions": [],
        "action_catalog": {
            "resume": {"title": "Release pause", "verb": "Release this pause", "short_verb": "Release…",
                       "icon": "play", "danger": False,
                       "effect": "Each pause is released on its own. On a finished run this releases it without restarting anything."},
            "cancel": {"title": "Cancel", "verb": "Cancel this work", "short_verb": "Cancel…",
                       "icon": "ban", "danger": True,
                       "effect": "The work is dropped from this run and counted as not covered."},
            "delay": {"title": "Delay", "verb": "Delay this work", "short_verb": "Delay…",
                      "icon": "forward", "danger": False,
                      "effect": "The work is offered to the next run that matches this one."},
            "edit": {"title": "Edit", "verb": "Apply edit", "short_verb": "Edit…",
                     "icon": "pencil", "danger": False,
                     "effect": "An edit reaches only unclaimed pending work and never widens the run's authority."},
        },
    }


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "item_id": "", "type": "job", "status": "completed", "wait_reason": "", "phase": "6",
        "purpose": "check", "agent_contract": "check", "origin": "engine", "executor": "agent",
        "queue_name": "", "queue_item": "", "objective": "", "operator_note": "", "skill": "",
        "scope": [], "inputs": [], "plan_rows": [], "required_outputs": [], "concurrency_key": "",
        "parent_job_id": "", "dependent_jobs": [], "lead_depth": 0, "deferred_from_run": "",
        "disposition": "", "disposition_reason": "", "attempt": 1, "futile_attempts": 0,
        "publication_failures": 0, "session_count": 1, "not_before": "", "updated_at": "",
        "created_at": "", "released_at": "", "actor": "", "reason": "", "work_subjects": [],
        "actions": [],
    }
    row.update(overrides)
    return row


def demo_page(*, run_id: str, digest: str, target_host: str, finished_at: str) -> dict[str, Any]:
    """One finished run's queue: what it covered, what it did not, and the pause still on record."""
    scope = [target_host]
    rows = [
        _row(item_id="row-floor-AUTH-MATRIX", status="completed", scope=scope, phase="6",
             objective="Execute plan row AUTH-MATRIX of the floor section.",
             inputs=["AUTH-MATRIX"], plan_rows=["AUTH-MATRIX"], updated_at=finished_at,
             work_subjects=[{"role": "outcome", "verdict_id": "demo-verdict-1",
                             "item": "Role matrix holds across the tenant boundary",
                             "target": target_host, "status": "safe",
                             "conclusion": "Every demoted role lost its permissions on the next request."}]),
        _row(item_id="row-floor-IDOR-EXPORT", status="completed", scope=scope, phase="6", skill="idor",
             objective="Execute plan row IDOR-EXPORT of the floor section.",
             inputs=["IDOR-EXPORT"], plan_rows=["IDOR-EXPORT"], updated_at=finished_at,
             work_subjects=[{"role": "outcome", "verdict_id": "demo-verdict-2",
                             "item": "Cross-tenant read through the export endpoint",
                             "target": target_host, "status": "vuln",
                             "conclusion": "One tenant read another tenant's export in full."}]),
        _row(item_id="queue-verify-demo-1", status="completed", agent_contract="verify",
             purpose="validate", queue_name="verify", queue_item="Cross-tenant read through the export endpoint",
             phase="7", scope=scope, updated_at=finished_at,
             objective="Independently validate the queued check."),
        _row(item_id="queue-report-demo-1", status="completed", agent_contract="report",
             purpose="report", queue_name="report", phase="8", scope=scope, updated_at=finished_at,
             objective="Write up the confirmed finding and attach its evidence."),
        _row(item_id="agent-lead-demo-session", status="failed", origin="agent", skill="session-management",
             scope=scope, phase="6", attempt=4, futile_attempts=3, wait_reason="retry-backoff",
             objective="Prove or disprove that a revoked session keeps answering inside the token cache window.",
             updated_at=finished_at),
        _row(item_id="row-explore-XSS-SEARCH", status="cancelled", scope=scope, phase="6",
             disposition="cancel", disposition_reason="Out of scope for this engagement window.",
             objective="Execute plan row XSS-SEARCH of the explore section.",
             inputs=["XSS-SEARCH"], plan_rows=["XSS-SEARCH"], attempt=0, session_count=0,
             updated_at=finished_at),
        _row(item_id="row-explore-SQLI-REPORTS", status="delayed", scope=scope, phase="6",
             disposition="delay", disposition_reason="Carried to the next run against this target.",
             objective="Execute plan row SQLI-REPORTS of the explore section.",
             inputs=["SQLI-REPORTS"], plan_rows=["SQLI-REPORTS"], attempt=0, session_count=0,
             updated_at=finished_at),
        _row(item_id="queue-prodval-demo-1", type="planned", status="pending", agent_contract="verify",
             purpose="validate", queue_name="prodval", phase="7", scope=scope, attempt=0,
             session_count=0, wait_reason="session-capacity",
             queue_item="Cross-tenant read through the export endpoint",
             objective="Re-test the confirmed finding against production and return a prod verdict.",
             updated_at=finished_at),
        _row(item_id="agent-lead-demo-invite", type="proposal", status="admission-rejected",
             origin="agent", skill="idor", scope=scope, attempt=0, session_count=0,
             objective="Chain the invite flow into an account takeover after the invite is withdrawn.",
             reason="The run's check budget was already spent when this lead arrived.",
             updated_at=finished_at),
        _row(item_id="hold-demo-1", type="hold", status="active", agent_contract="", purpose="",
             origin="operator", actor="operator:on-call", attempt=0, session_count=0,
             reason="Held the queue while the demo stand was redeployed.", created_at=finished_at,
             updated_at=finished_at,
             actions=[{"name": "resume", "method": "DELETE",
                       "href": f"runs/{run_id}/queue/holds/hold-demo-1",
                       "fields": [_field("reason", required=True,
                                         help="Kept in the queue history against the actor who sent it.")]}]),
    ]
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    return {
        "run_id": run_id,
        "revision": 42,
        "state": "paused",
        "run_state": "succeeded",
        "mutations_frozen": True,
        "descriptor_digest": digest,
        "total": len(rows),
        "uncovered": 2,
        "context_revision": 0,
        "next_offset": None,
        "counts": {
            "by_status": dict(sorted(by_status.items())),
            "planned": sum(row["type"] == "planned" for row in rows),
            "total": len(rows),
        },
        "items": rows,
        "history": [
            {"request_id": "demo-cancel-1", "actor": "operator:on-call", "action": "cancel",
             "item_id": "row-explore-XSS-SEARCH", "reason": "Out of scope for this engagement window.",
             "created_at": finished_at, "before_revision": 39, "revision": 40, "status": "applied"},
            {"request_id": "demo-delay-1", "actor": "operator:on-call", "action": "delay",
             "item_id": "row-explore-SQLI-REPORTS", "reason": "Carried to the next run against this target.",
             "created_at": finished_at, "before_revision": 40, "revision": 41, "status": "handoff-pending"},
        ],
        "handoffs": [],
    }
