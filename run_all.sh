#!/bin/bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
MODE_SEEN=0
DATABASE_SEEN=0

args=("$@")
i=0
while [ "$i" -lt "${#args[@]}" ]; do
    argument="${args[$i]}"
    case "$argument" in
        --mode)
            i=$((i + 1))
            [ "$i" -lt "${#args[@]}" ] || {
                echo "run_all.sh: --mode requires a value" >&2
                exit 2
            }
            MODE_SEEN=1
            ;;
        --mode=*)
            MODE_SEEN=1
            ;;
        --database)
            i=$((i + 1))
            [ "$i" -lt "${#args[@]}" ] || {
                echo "run_all.sh: --database requires a value" >&2
                exit 2
            }
            DATABASE_SEEN=1
            ;;
        --database=*)
            DATABASE_SEEN=1
            ;;
    esac
    i=$((i + 1))
done

if [ "$MODE_SEEN" -ne 1 ] || [ "$DATABASE_SEEN" -ne 1 ]; then
    echo "Usage: run_all.sh --mode MODE --database PATH [runner options]" >&2
    exit 2
fi

if [ -n "${PIPELINE_RUN_ID:-}" ] && [ -n "${RUN_ID:-}" ] \
    && [ "$PIPELINE_RUN_ID" != "$RUN_ID" ]; then
    echo "run_all.sh: PIPELINE_RUN_ID and RUN_ID conflict" >&2
    exit 2
fi

if [ -n "${PIPELINE_EFFECTIVE_DATE:-}" ] && [ -n "${EFFECTIVE_DATE:-}" ] \
    && [ "$PIPELINE_EFFECTIVE_DATE" != "$EFFECTIVE_DATE" ]; then
    echo "run_all.sh: PIPELINE_EFFECTIVE_DATE and EFFECTIVE_DATE conflict" >&2
    exit 2
fi

python_spec="${QUANT_PYTHON:-python3}"
read -r -a python_command <<< "$python_spec"
if [ "${#python_command[@]}" -eq 0 ]; then
    echo "run_all.sh: QUANT_PYTHON resolved to an empty command" >&2
    exit 2
fi

cd "$ROOT_DIR"
exec "${python_command[@]}" \
    "$ROOT_DIR/quant-strategy/scripts/daily_runner.py" \
    "$@"
