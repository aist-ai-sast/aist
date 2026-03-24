#!/usr/bin/env zsh
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")" && pwd)

cd "$ROOT_DIR"

clean=0
verbose=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean)
      clean=1
      shift
      ;;
    -v|--verbose)
      verbose=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./run-mcp-tests.zsh [--clean] [-v|--verbose]

Runs pytest inside the context-extractor-mcp Docker container.
Tests cover project_analysis and config_analysis MCP tools.

Options:
  --clean    Rebuild the image from scratch (no cache).
  -v         Verbose pytest output (-v flag).
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

IMAGE="aist-context-extractor-mcp"
BUILD_CONTEXT="./sast-combinator/context_extractor_service/ansible/files"

echo "==> Building ${IMAGE}..."
if [[ $clean -eq 1 ]]; then
  docker build --no-cache -t "${IMAGE}:test" "${BUILD_CONTEXT}"
else
  docker build -t "${IMAGE}:test" "${BUILD_CONTEXT}"
fi

PYTEST_ARGS="-x"
if [[ $verbose -eq 1 ]]; then
  PYTEST_ARGS="-xvs"
fi

echo "==> Running tests..."
docker run --rm \
  -w /app \
  "${IMAGE}:test" \
  python -m pytest tests/ ${PYTEST_ARGS}

echo "==> All tests passed."
