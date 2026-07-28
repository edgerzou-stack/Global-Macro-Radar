"""Verify the immutable artifacts produced by one production full-flow run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


class FullFlowVerificationError(RuntimeError):
    pass


def _load_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FullFlowVerificationError(
            f"Unable to read {label}: {path}"
        ) from error
    if not isinstance(payload, dict):
        raise FullFlowVerificationError(f"{label} must contain a JSON object")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise FullFlowVerificationError(f"Unable to read artifact: {path}") from error
    return digest.hexdigest()


def _bound_artifact(run_dir: Path, raw_path, label: str) -> Path:
    if not raw_path:
        raise FullFlowVerificationError(f"Prepared manifest has no {label} path")
    path = Path(raw_path).expanduser().resolve()
    if path.parent != run_dir:
        raise FullFlowVerificationError(
            f"{label} must be stored directly in the run directory: {path}"
        )
    if not path.is_file():
        raise FullFlowVerificationError(f"{label} is missing: {path}")
    return path


def verify_full_flow(
    run_manifest_path,
    journal_path,
    prepared_manifest_path,
    database_path,
):
    run_manifest_path = Path(run_manifest_path).expanduser().resolve()
    journal_path = Path(journal_path).expanduser().resolve()
    prepared_manifest_path = Path(prepared_manifest_path).expanduser().resolve()
    database_path = Path(database_path).expanduser().resolve()
    run_dir = run_manifest_path.parent

    if prepared_manifest_path.parent != run_dir:
        raise FullFlowVerificationError(
            "Prepared manifest is not bound to the pipeline run directory"
        )
    if journal_path.parent != run_dir / "delivery":
        raise FullFlowVerificationError(
            "Delivery journal is not bound to the pipeline run directory"
        )

    run_manifest = _load_json(run_manifest_path, "run manifest")
    if run_manifest.get("payload", {}).get("status") != "completed":
        raise FullFlowVerificationError(
            f"Run manifest is not completed: {run_manifest_path}"
        )

    prepared = _load_json(prepared_manifest_path, "prepared report manifest")
    if prepared.get("schema_version") != 2:
        raise FullFlowVerificationError(
            "Prepared report manifest must use schema version 2"
        )
    recipient_html = _bound_artifact(
        run_dir, prepared.get("html_path"), "prepared recipient HTML"
    )
    audit_html = _bound_artifact(
        run_dir, prepared.get("audit_html_path"), "prepared audit HTML"
    )
    recipient_sha = _sha256(recipient_html)
    audit_sha = _sha256(audit_html)
    if recipient_sha != prepared.get("html_sha256"):
        raise FullFlowVerificationError(
            "Prepared recipient HTML SHA does not match prepared manifest"
        )
    if audit_sha != prepared.get("audit_html_sha256"):
        raise FullFlowVerificationError(
            "Prepared audit HTML SHA does not match prepared manifest"
        )

    journal = _load_json(journal_path, "delivery journal")
    if journal.get("state") != "accepted_by_smtp":
        raise FullFlowVerificationError(
            "SMTP was not accepted; "
            f"journal state={journal.get('state')!r}"
        )
    if not journal.get("recipient"):
        raise FullFlowVerificationError("Delivery journal has no recipient")
    if journal.get("run_id") != run_dir.name:
        raise FullFlowVerificationError(
            "Delivery journal run ID does not match the run directory"
        )
    if journal.get("html_sha256") != recipient_sha:
        raise FullFlowVerificationError(
            "Prepared recipient HTML SHA does not match delivery journal"
        )
    if not database_path.is_file():
        raise FullFlowVerificationError(
            f"Production database is missing: {database_path}"
        )

    return {
        "run_id": journal["run_id"],
        "manifest": str(run_manifest_path),
        "prepared_manifest": str(prepared_manifest_path),
        "database_sha256": _sha256(database_path),
        "delivery_journal": str(journal_path),
        "delivery_state": journal["state"],
        "recipient": journal["recipient"],
        "report_html": str(recipient_html),
        "html_sha256": recipient_sha,
        "audit_report_html": str(audit_html),
        "audit_html_sha256": audit_sha,
        "note": (
            "accepted_by_smtp confirms SMTP acceptance only; "
            "it does not prove inbox receipt"
        ),
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Verify one completed production full-flow run"
    )
    parser.add_argument("run_manifest")
    parser.add_argument("delivery_journal")
    parser.add_argument("prepared_manifest")
    parser.add_argument("database")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    result = verify_full_flow(
        args.run_manifest,
        args.delivery_journal,
        args.prepared_manifest,
        args.database,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
