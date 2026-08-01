"""Verify the immutable artifacts produced by one production full-flow run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from send_unified_email import (
    EMAIL_DELIVERY_PROFILE,
    EMAIL_MIME_SCHEMA_VERSION,
    MAX_INLINE_IMAGE_BYTES,
    MAX_INLINE_IMAGE_WIDTH,
    MAX_LIVE_MESSAGE_BYTES,
)


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


def _verify_delivery_authorization(run_manifest, journal):
    manifest_value = (
        run_manifest.get("payload", {})
        .get("delivery_authorization", {})
        .get("duplicate_effective_date_override", False)
    )
    journal_value = journal.get(
        "duplicate_effective_date_override",
        False,
    )
    if type(manifest_value) is not bool or type(journal_value) is not bool:
        raise FullFlowVerificationError(
            "Duplicate effective-date delivery authorization must be boolean"
        )
    if manifest_value != journal_value:
        raise FullFlowVerificationError(
            "Run manifest and delivery journal disagree on duplicate "
            "effective-date authorization"
        )
    return manifest_value


def _verify_delivery_profile(prepared, journal):
    if prepared.get("mime_schema_version") != EMAIL_MIME_SCHEMA_VERSION:
        raise FullFlowVerificationError(
            "Prepared report does not use the current MIME schema"
        )
    if journal.get("mime_schema_version") != EMAIL_MIME_SCHEMA_VERSION:
        raise FullFlowVerificationError(
            "Delivery journal does not use the current MIME schema"
        )
    if prepared.get("delivery_profile") != EMAIL_DELIVERY_PROFILE:
        raise FullFlowVerificationError(
            "Prepared report does not use the mailbox-safe delivery profile"
        )
    if journal.get("delivery_profile") != EMAIL_DELIVERY_PROFILE:
        raise FullFlowVerificationError(
            "Delivery journal does not use the mailbox-safe delivery profile"
        )

    message_size = journal.get("message_size_bytes")
    message_limit = journal.get("message_size_limit_bytes")
    if (
        type(message_size) is not int
        or type(message_limit) is not int
        or message_size <= 0
        or message_limit != MAX_LIVE_MESSAGE_BYTES
        or message_size > message_limit
    ):
        raise FullFlowVerificationError(
            "Delivery journal has invalid mailbox-safe message-size evidence"
        )

    images = journal.get("inline_images")
    image_count = journal.get("inline_image_count")
    if not isinstance(images, list) or image_count != len(images):
        raise FullFlowVerificationError(
            "Delivery journal inline-image evidence is inconsistent"
        )
    delivered_total = 0
    source_total = 0
    for image in images:
        if not isinstance(image, dict):
            raise FullFlowVerificationError(
                "Delivery journal contains invalid inline-image evidence"
            )
        delivered_bytes = image.get("delivered_bytes")
        source_bytes = image.get("source_bytes")
        delivered_width = image.get("delivered_width")
        delivered_height = image.get("delivered_height")
        if (
            type(delivered_bytes) is not int
            or type(source_bytes) is not int
            or type(delivered_width) is not int
            or type(delivered_height) is not int
            or delivered_bytes <= 0
            or delivered_bytes > MAX_INLINE_IMAGE_BYTES
            or source_bytes <= 0
            or delivered_width <= 0
            or delivered_width > MAX_INLINE_IMAGE_WIDTH
            or delivered_height <= 0
        ):
            raise FullFlowVerificationError(
                "Delivery journal inline image exceeds the safe profile"
            )
        delivered_total += delivered_bytes
        source_total += source_bytes
    if (
        journal.get("inline_image_delivered_bytes") != delivered_total
        or journal.get("inline_image_source_bytes") != source_total
    ):
        raise FullFlowVerificationError(
            "Delivery journal inline-image totals are inconsistent"
        )
    return {
        "delivery_profile": EMAIL_DELIVERY_PROFILE,
        "mime_schema_version": EMAIL_MIME_SCHEMA_VERSION,
        "message_size_bytes": message_size,
        "message_size_limit_bytes": message_limit,
    }


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
    delivery_profile = _verify_delivery_profile(prepared, journal)
    authorized_resend = _verify_delivery_authorization(
        run_manifest,
        journal,
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
        "duplicate_effective_date_override": authorized_resend,
        **delivery_profile,
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
