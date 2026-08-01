#!/usr/bin/env python3
"""Verify branch-aware release coverage without hiding legacy modules."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


GLOBAL_BRANCH_FLOOR = 23.0
CRITICAL_MODULE_BRANCH_FLOORS = {
    "industry-radar/pipeline_delivery.py": 50.0,
    "industry-radar/pipeline_rendering.py": 50.0,
    "industry-radar/pipeline_selection.py": 50.0,
    "quant-strategy/scripts/core/run_context.py": 50.0,
    "quant-strategy/scripts/core/trade_intents.py": 50.0,
    "quant-strategy/scripts/daily_runner.py": 50.0,
    "quant-strategy/scripts/execute_pending_intents.py": 50.0,
    "quant-strategy/scripts/generate_report.py": 50.0,
    "quant-strategy/scripts/get_stock_name.py": 50.0,
    "quant-strategy/scripts/report_repository.py": 50.0,
    "quant-strategy/scripts/report_review_cache.py": 50.0,
    "quant-strategy/scripts/report_review_service.py": 50.0,
    "quant-strategy/scripts/send_unified_email.py": 50.0,
}


class ReleaseCoverageError(RuntimeError):
    pass


def _percentage(summary):
    try:
        return float(summary["percent_covered"])
    except (KeyError, TypeError, ValueError) as error:
        raise ReleaseCoverageError(
            "coverage summary has no numeric percent_covered"
        ) from error


def verify_release_coverage(payload):
    if not isinstance(payload, dict):
        raise ReleaseCoverageError("coverage payload must be a JSON object")
    totals = payload.get("totals")
    files = payload.get("files")
    if not isinstance(totals, dict) or not isinstance(files, dict):
        raise ReleaseCoverageError("coverage payload is missing totals/files")

    failures = []
    global_percentage = _percentage(totals)
    if global_percentage < GLOBAL_BRANCH_FLOOR:
        failures.append(
            f"global coverage {global_percentage:.2f}% is below "
            f"{GLOBAL_BRANCH_FLOOR:.2f}%"
        )

    critical_results = {}
    for path, floor in CRITICAL_MODULE_BRANCH_FLOORS.items():
        record = files.get(path)
        if not isinstance(record, dict):
            failures.append(f"critical module is absent from coverage: {path}")
            continue
        percentage = _percentage(record.get("summary"))
        critical_results[path] = percentage
        if percentage < floor:
            failures.append(
                f"critical module {path} coverage {percentage:.2f}% "
                f"is below {floor:.2f}%"
            )

    if failures:
        raise ReleaseCoverageError("; ".join(failures))
    return {
        "status": "ok",
        "global_percentage": global_percentage,
        "global_floor": GLOBAL_BRANCH_FLOOR,
        "critical_modules": critical_results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage_json")
    args = parser.parse_args(argv)
    path = Path(args.coverage_json).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseCoverageError(
            f"unable to read coverage report: {path}"
        ) from error
    result = verify_release_coverage(payload)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseCoverageError as error:
        raise SystemExit(f"Release coverage failed: {error}")
