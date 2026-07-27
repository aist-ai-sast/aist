# DAST staging compatibility canary

Use this runbook only after AIST and DAST are deployed to the target Ubuntu/Kali amd64 staging
environment. The canary scans real deployed targets, so an approved target owner and
change/security ticket are required. Do not substitute production targets or run it from an ARM
development machine.

## Inputs and safety boundary

Prepare outside Git:

- an AIST write PAT in `AIST_CANARY_TOKEN`;
- the provider-generated v2 onboarding bundle in a mode-`0600` file;
- an existing same-organization VPN integration ID;
- exact and ancestor project-version IDs for approved staging revisions;
- launch-config IDs for approved clean, finding, and cancellation fixtures;
- an approval reference identifying the target owner and allowed scan window.

The script accepts only an HTTPS AIST URL. Tokens are read from the environment or onboarding
bundle and are never written to evidence. Evidence contains IDs, normalized outcomes, expected
selection, and log byte count/SHA-256 only—not URLs, log contents, tokens, CA material, findings,
or request bodies.

## 1. Onboard and synchronize

Run from the checked-out AIST repository on the deployed host:

```bash
export AIST_CANARY_TOKEN='<write PAT from the approved secret store>'
python3 scripts/dast-staging-canary.py \
  --aist-url 'https://<aist-staging>' \
  --evidence /secure/canary/onboarding.json \
  onboard \
  --organization-id <organization-id> \
  --vpn-integration-id <vpn-integration-id> \
  --bundle /secure/canary/dast-onboarding-v2.json
```

The command imports only contract major 2, validates through the configured VPN, waits for
`READY`, synchronizes capabilities, and requires a non-empty catalog. If an active integration
already exists, update it through the normal onboarding UI/API after review; the canary never
disables it automatically.

Create or review project bindings and launch configs through the normal AIST UI. Every binding
must be ready, autonomous-enabled, use the expected repository key, and reference only a target
named in the approval. Do not put parameters, URLs, or tokens in the canary manifest.

## 2. Define approved cases

Create a mode-`0600` manifest. The exact key set is intentional:

```json
{
  "version": 1,
  "approval_ref": "SEC-0000",
  "project_id": 1,
  "cases": [
    {
      "name": "exact clean",
      "launch_config_id": 11,
      "project_version_id": 101,
      "expected_outcome": "SUCCESS_CLEAN",
      "expected_relation": "exact",
      "expected_distance": 0,
      "request_stop": false
    },
    {
      "name": "ancestor findings",
      "launch_config_id": 12,
      "project_version_id": 102,
      "expected_outcome": "SUCCESS_WITH_FINDINGS",
      "expected_relation": "ancestor",
      "expected_distance": 1,
      "request_stop": false
    },
    {
      "name": "confirmed cancel",
      "launch_config_id": 13,
      "project_version_id": 102,
      "expected_outcome": "CANCELLED",
      "expected_relation": "ancestor",
      "expected_distance": 1,
      "request_stop": true
    }
  ]
}
```

Use separate clean and findings fixtures. The findings fixture may contain only pre-approved,
reversible test behavior. The cancellation run must last long enough for AIST to persist stop
intent and receive a terminal provider confirmation.

## 3. Run the AIST cases

```bash
python3 scripts/dast-staging-canary.py \
  --aist-url 'https://<aist-staging>' \
  --timeout 3600 \
  --evidence /secure/canary/aist-result.json \
  run \
  --manifest /secure/canary/manifest.json
```

The command uses the public AIST v2 API to enqueue each launch config, waits through generic
queue/capacity allocation, waits for a cancellation case to enter running state before requesting
stop, polls terminal state, verifies normalized clean/findings/cancel outcomes, and downloads live
logs only to record their size and digest. This first artifact is marked `provider_verified:
false`; it is not sufficient to approve rollout.

## 4. Export and verify provider evidence

From the DAST operator surface, export a reviewed, secret-free record after the runs. It binds
provider run IDs to AIST correlation IDs and includes selection. Also perform the negative route
check: a connector without the VPN namespace/trusted-VPN flag must reject the private gateway
before creating an HTTP pool.

```json
{
  "version": 1,
  "direct_non_vpn_blocked": true,
  "runs": [
    {
      "correlation_id": "<AIST pipeline id>",
      "run_id": "<DAST run id>",
      "relation": "exact",
      "distance": 0
    }
  ]
}
```

Include one entry per manifest case. Do not include target URLs, source paths, evidence text,
headers, credentials, or raw logs.

```bash
python3 scripts/dast-staging-canary.py \
  --evidence /secure/canary/result.json \
  verify \
  --aist-evidence /secure/canary/aist-result.json \
  --provider-evidence /secure/canary/provider.json
```

Verification requires exact provider-side correlation/run ID and relation/distance matches plus
the non-VPN denial, then marks the final artifact `provider_verified: true`.

Retain `onboarding.json`, `aist-result.json`, `result.json`, reviewed provider JSON, deployment
revisions, and CI links under the approval ticket's retention policy. A mismatch, timeout, missing
queue item, non-terminal stop, provider evidence gap, or API error is a failed rollout gate.

## Target-host completion checklist

- [ ] Clean AIST deployment succeeds twice on the same amd64 staging state.
- [ ] DAST v2 validation and catalog sync traverse only the explicit VPN integration.
- [ ] Exact, ancestor, clean, findings, and cancellation cases match on both sides.
- [ ] Live log digest is non-empty and no secret/log content appears in retained evidence.
- [ ] Direct non-VPN gateway access is denied before HTTP connection setup.
- [ ] Correlation IDs, run IDs, revisions, and CI links are attached to the approval ticket.
