#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROVIDER_DIR="${1:?usage: scripts/update-dast-contract.sh /path/to/dast/Claude_Project_Folder}"
PROVIDER_ARTIFACT="${PROVIDER_DIR}/contracts/dast-integration.openapi.json"
CONSUMER_DIR="${PROJECT_DIR}/sast-combinator/sast-pipeline/contracts"
CONSUMER_ARTIFACT="${CONSUMER_DIR}/dast-integration.openapi.json"
SOURCE_RECORD="${CONSUMER_DIR}/dast-integration.source.json"

test -f "${PROVIDER_ARTIFACT}"
mkdir -p "${CONSUMER_DIR}"
cp "${PROVIDER_ARTIFACT}" "${CONSUMER_ARTIFACT}"

artifact_sha256="$(shasum -a 256 "${PROVIDER_ARTIFACT}" | awk '{print $1}')"
provider_revision="$(git -C "${PROVIDER_DIR}" rev-parse HEAD)"
jq --null-input \
  --arg artifact "dast-integration.openapi.json" \
  --arg artifact_sha256 "${artifact_sha256}" \
  --arg provider_artifact "contracts/dast-integration.openapi.json" \
  --arg provider_git_revision "${provider_revision}" \
  --arg provider_repository "dast/Claude_Project_Folder" \
  '{
    artifact: $artifact,
    artifact_sha256: $artifact_sha256,
    provider_artifact: $provider_artifact,
    provider_git_revision: $provider_git_revision,
    provider_repository: $provider_repository
  }' > "${SOURCE_RECORD}"

docker build \
  --target test \
  --tag aist-dast-contract-check:local \
  --file "${PROJECT_DIR}/sast-combinator/sast-pipeline/Dockerfiles/dast_connector/Dockerfile" \
  "${PROJECT_DIR}/sast-combinator/sast-pipeline"
docker run --rm aist-dast-contract-check:local \
  tests/pipeline_tests/test_dast_contract_snapshot.py -p no:cacheprovider -q
