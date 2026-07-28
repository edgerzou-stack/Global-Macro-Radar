#!/bin/bash
set -euo pipefail
umask 077

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="$ROOT_DIR/.venv/bin/python"
DATABASE="$ROOT_DIR/quant-strategy/quant_system.db"
ARTIFACT_ROOT="$ROOT_DIR/reports/pipeline-runs"

if [ ! -x "$PYTHON" ]; then
    echo "Missing project interpreter: $PYTHON" >&2
    exit 2
fi

"$PYTHON" - "$DATABASE" <<'PY'
import sqlite3
import sys
from pathlib import Path

database = Path(sys.argv[1]).resolve()
if sys.version_info[:3] != (3, 11, 9):
    raise SystemExit(
        "Production requires Python 3.11.9; got "
        + ".".join(map(str, sys.version_info[:3]))
    )
if not database.is_file():
    raise SystemExit(f"Missing production database: {database}")
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
try:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("Production database integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SystemExit("Production database foreign_key_check failed")
    if connection.execute("PRAGMA user_version").fetchone()[0] != 8:
        raise SystemExit("Production database must use schema v8")
    environment = connection.execute(
        "SELECT value FROM meta_data WHERE key='database_environment'"
    ).fetchone()
    if environment is None or environment[0] != "production":
        raise SystemExit("Canonical database is not labelled production")
    retirement = connection.execute(
        "SELECT value FROM meta_data WHERE key='strategy_retirement_v1'"
    ).fetchone()
    if retirement is None:
        raise SystemExit("Approved v4 strategy-retirement cleanup is not applied")
finally:
    connection.close()
PY

if [ "${1:-}" = "--preflight-only" ]; then
    if [ "$#" -ne 1 ]; then
        echo "--preflight-only accepts no additional arguments" >&2
        exit 2
    fi
    echo "Production full-flow preflight: OK"
    exit 0
fi
if [ "$#" -ne 0 ]; then
    echo "Usage: $0 [--preflight-only]" >&2
    exit 2
fi

read -r EFFECTIVE_DATE RUN_SUFFIX <<EOF
$("$PYTHON" - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo

now = datetime.now(ZoneInfo("Asia/Shanghai"))
print(now.date().isoformat(), now.strftime("%Y%m%dT%H%M%S%f"))
PY
)
EOF

RUN_ID="production-fullflow-${RUN_SUFFIX}"
RUN_DIR="$ARTIFACT_ROOT/$RUN_ID"
MANIFEST="$RUN_DIR/run-manifest.json"
JOURNAL="$RUN_DIR/delivery/$RUN_ID.json"
PREPARED_MANIFEST="$RUN_DIR/prepared-report.json"

echo "Starting one production run: $RUN_ID"
"$ROOT_DIR/run_all.sh" \
    --mode production \
    --database "$DATABASE" \
    --confirm-production-writes \
    --effective-date "$EFFECTIVE_DATE" \
    --run-id "$RUN_ID" \
    --artifact-root "$ARTIFACT_ROOT" \
    --delivery-mode live \
    --confirm-live-delivery

"$PYTHON" "$ROOT_DIR/quant-strategy/scripts/verify_production_full_flow.py" \
    "$MANIFEST" \
    "$JOURNAL" \
    "$PREPARED_MANIFEST" \
    "$DATABASE"
