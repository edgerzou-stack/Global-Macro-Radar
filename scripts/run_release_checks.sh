#!/bin/bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
COVERAGE_JSON="$ROOT_DIR/reports/release-coverage.json"

if [ ! -x "$PYTHON" ]; then
    echo "Missing project interpreter: $PYTHON" >&2
    exit 2
fi

mkdir -p "$ROOT_DIR/reports"
cd "$ROOT_DIR"

"$PYTHON" -m pytest -q -p no:cacheprovider \
    -m "not live and not llm_eval and not slow" \
    --disable-socket --allow-unix-socket \
    --cov=quant-strategy/scripts \
    --cov=industry-radar \
    --cov-branch \
    --cov-report=json:"$COVERAGE_JSON"

"$PYTHON" "$ROOT_DIR/scripts/check_release_coverage.py" "$COVERAGE_JSON"
git diff --check
