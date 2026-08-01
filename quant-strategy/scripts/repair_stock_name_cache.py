"""Repair display-name cache from hash-verified completed report artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from get_stock_name import CACHE_FILE, StockNameResolver, _usable_name


SYMBOL_PATTERN = re.compile(
    r"(?:\d{6}|\d{4}\.HK|[A-Z][A-Z0-9.-]{0,9})"
)
COMBINED_NAME_PATTERN = re.compile(
    r"^(?P<symbol>\d{6}|\d{4}\.HK|[A-Z][A-Z0-9.-]{0,9})"
    r"\s+\((?P<name>[^()]+)\)$"
)
NON_NAME_PATTERN = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}|[-+]?\d+(?:\.\d+)?%?|N/A|-)"
)
RESERVED_MARKET_LABELS = {"A", "HK", "US"}


class StockNameRepairError(RuntimeError):
    pass


def _normalize_display_name(name: str) -> str:
    collapsed = " ".join(str(name).split())
    return re.sub(
        r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])",
        "",
        collapsed,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _merge_name(result: dict[str, str], symbol: str, raw_name: str) -> None:
    if symbol in RESERVED_MARKET_LABELS:
        return
    name = _usable_name(symbol, _normalize_display_name(raw_name))
    if name is None or SYMBOL_PATTERN.fullmatch(name) or NON_NAME_PATTERN.fullmatch(name):
        return
    previous = result.get(symbol)
    if previous is not None and previous != name:
        previous_folded = previous.casefold()
        name_folded = name.casefold()
        if name_folded.startswith(previous_folded):
            result[symbol] = name
            return
        if previous_folded.startswith(name_folded):
            return
        raise StockNameRepairError(
            f"Conflicting verified names for {symbol}: {previous!r} != {name!r}"
        )
    result[symbol] = name


def extract_names(html_text: str) -> dict[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    result: dict[str, str] = {}
    for row in soup.select("tr"):
        cells = [
            cell.get_text(" ", strip=True)
            for cell in row.find_all(["td", "th"])
        ]
        for index, symbol in enumerate(cells[:-1]):
            if SYMBOL_PATTERN.fullmatch(symbol):
                _merge_name(result, symbol, cells[index + 1])
        for text in cells:
            match = COMBINED_NAME_PATTERN.fullmatch(text)
            if match:
                _merge_name(
                    result,
                    match.group("symbol"),
                    match.group("name"),
                )
    return result


def _load_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StockNameRepairError(
            f"Unreadable {label} {path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise StockNameRepairError(f"{label} is not an object: {path}")
    return payload


def _legacy_report_evidence(
    manifest_path: Path,
    envelope: dict,
) -> tuple[Path, str]:
    run = envelope.get("run")
    run_id = str(run.get("run_id") or "") if isinstance(run, dict) else ""
    if not run_id or manifest_path.parent.name != run_id:
        raise StockNameRepairError(
            f"Legacy manifest run identity is invalid: {manifest_path}"
        )
    prepared_path = manifest_path.parent / "prepared-report.json"
    journal_path = manifest_path.parent / "delivery" / f"{run_id}.json"
    prepared = _load_json(prepared_path, "prepared report manifest")
    journal = _load_json(journal_path, "delivery journal")
    try:
        report_path = Path(prepared["html_path"]).expanduser().resolve()
        prepared_sha256 = str(prepared["html_sha256"]).lower()
        journal_sha256 = str(journal["html_sha256"]).lower()
        journal_run_id = str(journal["run_id"])
        journal_state = str(journal["state"])
    except KeyError as error:
        raise StockNameRepairError(
            f"Legacy report evidence is incomplete for {run_id}"
        ) from error
    if report_path.parent != manifest_path.parent.resolve():
        raise StockNameRepairError(
            f"Legacy prepared report escapes its run directory: {report_path}"
        )
    if (
        journal_run_id != run_id
        or journal_sha256 != prepared_sha256
        or journal_state
        not in {"sink", "accepted_by_smtp", "confirmed_received", "delivered"}
    ):
        raise StockNameRepairError(
            f"Legacy prepared report and delivery journal disagree for {run_id}"
        )
    return report_path, prepared_sha256


def names_from_manifest(manifest_path: Path) -> dict[str, str]:
    envelope = _load_json(manifest_path, "manifest")
    payload = envelope.get("payload")
    if (
        envelope.get("artifact_type") != "pipeline-run-manifest"
        or not isinstance(payload, dict)
        or payload.get("status") != "completed"
    ):
        raise StockNameRepairError(
            f"Manifest is not a completed pipeline run: {manifest_path}"
        )
    try:
        evidence = payload["stage_inputs"]["report_delivery"][
            "prepared_recipient_html"
        ]
        report_path = Path(evidence["path"]).expanduser().resolve()
        expected_sha256 = str(evidence["sha256"]).lower()
    except (KeyError, TypeError):
        report_path, expected_sha256 = _legacy_report_evidence(
            manifest_path,
            envelope,
        )
    if not report_path.is_file():
        raise StockNameRepairError(
            f"Prepared recipient HTML is missing: {report_path}"
        )
    actual_sha256 = _sha256(report_path)
    if actual_sha256 != expected_sha256:
        raise StockNameRepairError(
            "Prepared recipient HTML SHA-256 mismatch: "
            f"expected={expected_sha256}, actual={actual_sha256}"
        )
    return extract_names(report_path.read_text(encoding="utf-8"))


def recover_names(manifest_paths: list[Path]) -> dict[str, str]:
    recovered: dict[str, str] = {}
    for manifest_path in manifest_paths:
        for symbol, name in names_from_manifest(manifest_path).items():
            _merge_name(recovered, symbol, name)
    return recovered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recover stock names from completed, hash-verified recipient reports"
        )
    )
    parser.add_argument(
        "manifests",
        nargs="+",
        type=Path,
        help="Completed pipeline run manifests",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=CACHE_FILE,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Atomically update the local cache; default is dry-run",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    names = recover_names(
        [path.expanduser().resolve() for path in args.manifests]
    )
    if not names:
        raise StockNameRepairError(
            "Verified reports contain no usable stock names"
        )
    if args.apply:
        resolver = StockNameResolver(args.cache_file)
        resolver.prime(names, persist=True)
        resolver.flush()
    print(
        json.dumps(
            {
                "applied": bool(args.apply),
                "cache_file": str(args.cache_file.expanduser().resolve()),
                "recovered_name_count": len(names),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
