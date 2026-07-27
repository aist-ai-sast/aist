#!/usr/bin/env python3
# ruff: noqa: EM101, EM102, S310, T201, TRY003
"""Run the credential-safe portions of the real AIST/DAST staging canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API_PREFIX = "/api/v2/aist"
TERMINAL_PIPELINE_STATES = {"FINISHED", "FINISHED_WITH_WARNINGS"}
TERMINAL_REQUEST_STATES = {"FAILED", "EXPIRED", "SUPERSEDED", "CANCELLED"}
ALLOWED_OUTCOMES = {"SUCCESS_CLEAN", "SUCCESS_WITH_FINDINGS", "CANCELLED"}


class CanaryError(RuntimeError):
    pass


class AistClient:
    def __init__(self, *, base_url: str, token: str, timeout: float = 30.0):
        if not base_url.startswith("https://"):
            raise CanaryError("AIST base URL must use HTTPS.")
        if not token or token != token.strip():
            raise CanaryError("AIST_CANARY_TOKEN must be non-empty with no surrounding whitespace.")
        self.api_root = f"{base_url.rstrip('/')}{API_PREFIX}"
        self.timeout = timeout
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._ssl_context = ssl.create_default_context()

    def request(self, method: str, path: str, payload: dict | None = None) -> Any:
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.api_root}/{path.lstrip('/')}",
            data=body,
            headers=self._headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl_context) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise CanaryError(f"AIST API rejected {method} {path} with HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise CanaryError(f"AIST API request failed for {method} {path}.") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise CanaryError(f"AIST API returned invalid JSON for {method} {path}.") from exc

    def get_text(self, path: str) -> bytes:
        request = urllib.request.Request(
            f"{self.api_root}/{path.lstrip('/')}",
            headers={"Accept": "text/plain", "Authorization": self._headers["Authorization"]},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl_context) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            raise CanaryError(f"AIST API log request failed for {path}.") from exc


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CanaryError(f"Cannot read valid JSON from {path}.") from exc
    if not isinstance(value, dict):
        raise CanaryError(f"Expected a JSON object in {path}.")
    return value


def _require_exact_keys(value: dict, expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise CanaryError(f"{label} must contain exactly: {', '.join(sorted(expected))}.")


def _poll(getter, predicate, *, timeout: float, interval: float, label: str):
    deadline = time.monotonic() + timeout
    while True:
        value = getter()
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise CanaryError(f"Timed out waiting for {label}.")
        time.sleep(interval)


def onboard(client: AistClient, args) -> dict:
    bundle = _load_json(args.bundle)
    imported = client.request(
        "POST",
        f"organizations/{args.organization_id}/dast-integration/import/",
        {
            "name": args.name,
            "vpn_integration_id": args.vpn_integration_id,
            "bundle": bundle,
        },
    )
    integration_id = imported.get("id") if isinstance(imported, dict) else None
    if not isinstance(integration_id, int):
        raise CanaryError("Onboarding response did not contain an integration id.")

    validation = client.request("POST", f"integrations/{integration_id}/validate/")
    task_id = validation.get("task_id") if isinstance(validation, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise CanaryError("Validation response did not contain a task id.")
    validation_result = _poll(
        lambda: client.request("GET", f"integrations/{integration_id}/validate/{task_id}/"),
        lambda value: value.get("state") in {"READY", "INVALID"},
        timeout=args.timeout,
        interval=args.poll_interval,
        label="DAST integration validation",
    )
    if validation_result.get("state") != "READY" or validation_result.get("valid") is not True:
        raise CanaryError("DAST integration validation did not reach READY.")

    client.request("POST", f"dast-integrations/{integration_id}/sync-capabilities/")
    integration = _poll(
        lambda: client.request("GET", f"dast-integrations/{integration_id}/onboarding/"),
        lambda value: bool((value.get("dast_state") or {}).get("capabilities_synced_at"))
        or bool((value.get("dast_state") or {}).get("sync_error_code")),
        timeout=args.timeout,
        interval=args.poll_interval,
        label="DAST capability synchronization",
    )
    state = integration.get("dast_state") or {}
    if state.get("sync_error_code"):
        raise CanaryError("DAST capability synchronization failed.")
    targets = client.request("GET", f"organizations/{args.organization_id}/dast-targets/")
    if not isinstance(targets, list) or not targets:
        raise CanaryError("DAST capability catalog is empty.")
    return {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "integration_id": integration_id,
        "validation_state": state.get("validation_state"),
        "contract_version": state.get("contract_version"),
        "capabilities_synced": True,
        "target_ids": sorted(target.get("provider_id") for target in targets if target.get("provider_id")),
    }


def _load_run_manifest(path: Path) -> dict:
    manifest = _load_json(path)
    _require_exact_keys(manifest, {"version", "approval_ref", "project_id", "cases"}, label="canary manifest")
    if manifest["version"] != 1 or not isinstance(manifest["approval_ref"], str) or not manifest["approval_ref"].strip():
        raise CanaryError("Canary manifest requires version 1 and a non-empty approval_ref.")
    if not isinstance(manifest["project_id"], int) or not isinstance(manifest["cases"], list) or not manifest["cases"]:
        raise CanaryError("Canary manifest requires project_id and at least one case.")
    expected_keys = {
        "name",
        "launch_config_id",
        "project_version_id",
        "expected_outcome",
        "expected_relation",
        "expected_distance",
        "request_stop",
    }
    for case in manifest["cases"]:
        if not isinstance(case, dict):
            raise CanaryError("Every canary case must be an object.")
        _require_exact_keys(case, expected_keys, label="canary case")
        if case["expected_outcome"] not in ALLOWED_OUTCOMES:
            raise CanaryError("Canary case has an unsupported expected_outcome.")
        if case["expected_relation"] not in {"exact", "ancestor"}:
            raise CanaryError("Canary case relation must be exact or ancestor.")
        if not isinstance(case["expected_distance"], int) or case["expected_distance"] < 0:
            raise CanaryError("Canary expected_distance must be a non-negative integer.")
        if case["expected_relation"] == "exact" and case["expected_distance"] != 0:
            raise CanaryError("Exact canary selection must have distance 0.")
    return manifest


def _queue_item(client: AistClient, request_id: int) -> dict:
    response = client.request("GET", "launch-requests/?limit=2000")
    results = response.get("results") if isinstance(response, dict) else None
    if not isinstance(results, list):
        raise CanaryError("Launch request list response is invalid.")
    item = next((candidate for candidate in results if candidate.get("id") == request_id), None)
    if item is None:
        raise CanaryError("Canary launch request is no longer visible in the authorized list.")
    return item


def _run_case(client: AistClient, project_id: int, case: dict, args) -> dict:
    started = client.request(
        "POST",
        f"projects/{project_id}/launch-configs/{case['launch_config_id']}/start/",
        {"project_version_id": case["project_version_id"]},
    )
    request_id = started.get("id") if isinstance(started, dict) else None
    if not isinstance(request_id, int):
        raise CanaryError("Launch response did not contain a request id.")
    queue_item = _poll(
        lambda: _queue_item(client, request_id),
        lambda value: bool(value.get("pipeline_id")) or value.get("state") in TERMINAL_REQUEST_STATES,
        timeout=args.timeout,
        interval=args.poll_interval,
        label=f"pipeline allocation for {case['name']}",
    )
    pipeline_id = queue_item.get("pipeline_id")
    if not isinstance(pipeline_id, str) or not pipeline_id:
        raise CanaryError(f"Canary request {case['name']} terminated before pipeline allocation.")
    if case["request_stop"]:
        _poll(
            lambda: client.request("GET", f"pipelines/{pipeline_id}"),
            lambda value: value.get("status") == "SAST_LAUNCHED"
            or value.get("status") in TERMINAL_PIPELINE_STATES,
            timeout=args.timeout,
            interval=args.poll_interval,
            label=f"running pipeline for {case['name']}",
        )
        client.request("POST", f"pipelines/{pipeline_id}/stop/")
    pipeline = _poll(
        lambda: client.request("GET", f"pipelines/{pipeline_id}"),
        lambda value: value.get("status") in TERMINAL_PIPELINE_STATES,
        timeout=args.timeout,
        interval=args.poll_interval,
        label=f"terminal pipeline for {case['name']}",
    )
    if pipeline.get("dast_outcome_code") != case["expected_outcome"]:
        raise CanaryError(f"Canary case {case['name']} returned an unexpected outcome.")
    logs = client.get_text(f"pipelines/{pipeline_id}/logs/download/")
    return {
        "name": case["name"],
        "request_id": request_id,
        "pipeline_id": pipeline_id,
        "external_run_id": pipeline.get("external_run_id"),
        "status": pipeline.get("status"),
        "outcome": pipeline.get("dast_outcome_code"),
        "expected_relation": case["expected_relation"],
        "expected_distance": case["expected_distance"],
        "logs_bytes": len(logs),
        "logs_sha256": hashlib.sha256(logs).hexdigest(),
    }


def _verify_provider_evidence(cases: list[dict], path: Path) -> None:
    evidence = _load_json(path)
    _require_exact_keys(evidence, {"version", "direct_non_vpn_blocked", "runs"}, label="provider evidence")
    if evidence["version"] != 1 or evidence["direct_non_vpn_blocked"] is not True:
        raise CanaryError("Provider evidence must confirm that direct non-VPN access was blocked.")
    runs = evidence["runs"]
    if not isinstance(runs, list):
        raise CanaryError("Provider evidence runs must be a list.")
    for case in cases:
        run = next(
            (
                candidate
                for candidate in runs
                if candidate.get("correlation_id") == case["pipeline_id"]
                and candidate.get("run_id") == case["external_run_id"]
            ),
            None,
        )
        if run is None:
            raise CanaryError(f"Provider evidence is missing case {case['name']}.")
        if run.get("relation") != case["expected_relation"] or run.get("distance") != case["expected_distance"]:
            raise CanaryError(f"Provider selection evidence does not match case {case['name']}.")


def run_canary(client: AistClient, args) -> dict:
    manifest = _load_run_manifest(args.manifest)
    cases = [_run_case(client, manifest["project_id"], case, args) for case in manifest["cases"]]
    return {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "approval_ref": manifest["approval_ref"],
        "direct_non_vpn_blocked": False,
        "provider_verified": False,
        "cases": cases,
    }


def verify_canary(args) -> dict:
    evidence = _load_json(args.aist_evidence)
    _require_exact_keys(
        evidence,
        {
            "version",
            "generated_at",
            "approval_ref",
            "direct_non_vpn_blocked",
            "provider_verified",
            "cases",
        },
        label="AIST canary evidence",
    )
    if evidence["version"] != 1 or evidence["provider_verified"] is not False:
        raise CanaryError("AIST canary evidence is not an unverified version 1 artifact.")
    if not isinstance(evidence["cases"], list) or not evidence["cases"]:
        raise CanaryError("AIST canary evidence must contain at least one case.")
    _verify_provider_evidence(evidence["cases"], args.provider_evidence)
    return {
        **evidence,
        "verified_at": datetime.now(UTC).isoformat(),
        "direct_non_vpn_blocked": True,
        "provider_verified": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aist-url")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--evidence", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    onboard_parser = subparsers.add_parser("onboard")
    onboard_parser.add_argument("--organization-id", type=int, required=True)
    onboard_parser.add_argument("--vpn-integration-id", type=int, required=True)
    onboard_parser.add_argument("--name", default="DAST staging canary")
    onboard_parser.add_argument("--bundle", type=Path, required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--aist-evidence", type=Path, required=True)
    verify_parser.add_argument("--provider-evidence", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "verify":
        evidence = verify_canary(args)
    else:
        if not args.aist_url:
            raise CanaryError("--aist-url is required for onboard and run.")
        token = os.environ.get("AIST_CANARY_TOKEN", "")
        client = AistClient(base_url=args.aist_url, token=token)
        evidence = onboard(client, args) if args.command == "onboard" else run_canary(client, args)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote redacted canary evidence to {args.evidence}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CanaryError as exc:
        print(f"DAST staging canary failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
