#!/usr/bin/env python3
"""Read-only audit of official HKEX financial-statement coverage."""

import argparse
import concurrent.futures
import json
from collections import Counter
from datetime import date
from pathlib import Path

import hkex_financials
from free_financials import (
    FinancialDataUnavailableError,
    FinancialNormalizationError,
    FinancialSourceError,
    normalize_cumulative_observations_detailed,
    observations_to_dataframe,
    validate_statement_frame,
)


ROOT = Path(__file__).resolve().parents[1]


def _audit_ticker(ticker: str, as_of_date: date) -> dict:
    try:
        observations = hkex_financials.load_hkex_financials(ticker, as_of_date)
        normalization = normalize_cumulative_observations_detailed(observations)
        normalization.raise_for_blocking_issues()
        frame = observations_to_dataframe(
            normalization.observations,
            normalization_diagnostics=normalization,
        )
        validate_statement_frame(frame)
        return {
            "ticker": ticker,
            "status": "usable",
            "reporting_frequency": frame.attrs["reporting_frequency"],
            "periods": [str(column.date()) for column in frame.columns],
            "source_documents": frame.attrs["source_documents"],
            "raw_observation_count": len(observations),
            "normalized_period_count": len(normalization.observations),
            "normalization_quarantined_issues": [
                issue.as_dict() for issue in normalization.quarantined_issues
            ],
        }
    except (FinancialDataUnavailableError, FinancialNormalizationError) as exc:
        return {
            "ticker": ticker,
            "status": "financial_data_unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    except FinancialSourceError as exc:
        # A validation failure after successful parsing is company-data
        # unavailability; loader/transport failures remain source errors.
        unavailable_prefixes = (
            "empty dataframe",
            "Insufficient dated statement columns",
            "Missing Total Revenue or Net Income",
        )
        status = (
            "financial_data_unavailable"
            if str(exc).startswith(unavailable_prefixes)
            else "source_error"
        )
        return {
            "ticker": ticker,
            "status": status,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    except Exception as exc:
        return {
            "ticker": ticker,
            "status": "source_error",
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _write_report(output_dir: Path, as_of_date: date, rows: list) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    counts = Counter(row["status"] for row in rows)
    payload = {
        "schema_version": 1,
        "effective_date": as_of_date.isoformat(),
        "source": "hkexnews_official",
        "attempted": len(rows),
        "counts": dict(sorted(counts.items())),
        "results": rows,
    }
    (output_dir / "hkex-financial-audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# HKEX Official Financial Audit",
        "",
        f"- Effective date: `{as_of_date.isoformat()}`",
        f"- Attempted: `{len(rows)}`",
        f"- Usable: `{counts['usable']}`",
        f"- Financial data unavailable: `{counts['financial_data_unavailable']}`",
        f"- Source errors: `{counts['source_error']}`",
        "",
        "| Ticker | Status | Frequency | Periods / reason |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        detail = ", ".join(row.get("periods", [])) or row.get("reason", "")
        detail = detail.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {row['ticker']} | {row['status']} | "
            f"{row.get('reporting_frequency', '')} | {detail} |"
        )
    (output_dir / "hkex-financial-audit.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--effective-date", required=True, type=date.fromisoformat)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--max-documents", type=int, default=8)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "artifacts" / "hkex-audit"
    )
    args = parser.parse_args()
    if args.workers <= 0 or args.max_documents <= 0:
        parser.error("--workers and --max-documents must be positive")
    hkex_financials.MAX_RESULT_DOCUMENTS = args.max_documents

    universes = json.loads((ROOT / "universes.json").read_text(encoding="utf-8"))
    tickers = list(dict.fromkeys(str(item) for item in universes["HK"]))
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_audit_ticker, ticker, args.effective_date): ticker
            for ticker in tickers
        }
        for completed, future in enumerate(
            concurrent.futures.as_completed(futures), start=1
        ):
            rows.append(future.result())
            if completed % 5 == 0 or completed == len(tickers):
                progress = Counter(row["status"] for row in rows)
                print(
                    json.dumps(
                        {
                            "completed": completed,
                            "attempted": len(tickers),
                            "counts": progress,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    rows.sort(key=lambda row: row["ticker"])
    _write_report(args.output_dir, args.effective_date, rows)
    counts = Counter(row["status"] for row in rows)
    print(json.dumps({"attempted": len(rows), "counts": counts}, ensure_ascii=False))
    return 1 if counts["source_error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
