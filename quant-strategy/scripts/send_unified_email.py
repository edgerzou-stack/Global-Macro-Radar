"""Build and deliver the unified report through an explicit delivery mode.

Non-production runs use the local sink. Live delivery is journaled by run ID.
``accepted_by_smtp`` means only that the outbound server reported no immediate
recipient refusal; it does not claim inbox delivery. An interrupted ``sending``
state is deliberately ambiguous and therefore requires operator reconciliation
rather than risking a duplicate message.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import format_datetime
from pathlib import Path

import markdown
import yaml
from dotenv import load_dotenv


VALID_DELIVERY_MODES = {"disabled", "sink", "live"}
DELIVERY_JOURNAL_SCHEMA_VERSION = 2
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
DATA_IMAGE_PATTERN = re.compile(
    r"data:image/(?P<subtype>[A-Za-z0-9.+-]+);base64,"
    r"(?P<payload>[A-Za-z0-9+/=\s]+)"
)


class DeliveryError(RuntimeError):
    pass


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_journal(path: Path, payload: dict) -> None:
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )


@contextmanager
def _delivery_lock(path: Path):
    """Serialize journal checks and side effects for one run across processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def get_latest_radar_report(reports_dir=None, effective_date=None):
    default_reports_dir = Path(__file__).resolve().parents[2] / "industry-radar" / "reports"
    directory = Path(
        reports_dir or os.environ.get("RADAR_REPORTS_DIR", default_reports_dir)
    )
    date_text = effective_date or os.environ.get("PIPELINE_EFFECTIVE_DATE") or os.environ.get(
        "EFFECTIVE_DATE"
    )
    if date_text:
        try:
            date.fromisoformat(date_text)
        except ValueError as error:
            raise DeliveryError(
                f"Invalid report effective date {date_text!r}; expected YYYY-MM-DD"
            ) from error
        report = directory / f"industry_report_{date_text}.md"
        if not report.is_file():
            raise DeliveryError(
                f"Radar report for effective date {date_text} does not exist: {report}"
            )
        return str(report)
    reports = sorted(directory.glob("*.md"), reverse=True)
    return str(reports[0]) if reports else None


def _load_delivery_config(config_path=None):
    default_path = Path(__file__).resolve().parents[2] / "industry-radar" / "config.yaml"
    path = Path(config_path or os.environ.get("RADAR_CONFIG", default_path))
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("delivery", {})


def _load_runtime_environment(env_path=None):
    explicit = env_path or os.environ.get("RADAR_ENV")
    path = Path(explicit) if explicit else Path(__file__).resolve().parents[2] / ".env"
    if path.is_file():
        load_dotenv(path, override=False)
        return path
    if explicit:
        raise DeliveryError(f"Configured RADAR_ENV does not exist: {path}")
    return None


def load_approved_html(path, expected_sha256=None):
    html_path = Path(path).expanduser().resolve()
    if not html_path.is_file():
        raise DeliveryError(f"Approved HTML file does not exist: {html_path}")
    try:
        html_content = html_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise DeliveryError(f"Unable to read approved HTML file: {html_path}") from error
    digest = hashlib.sha256(html_content.encode("utf-8")).hexdigest()
    if expected_sha256 is not None:
        if not SHA256_PATTERN.fullmatch(expected_sha256):
            raise DeliveryError("Expected HTML SHA-256 must contain exactly 64 hex digits")
        if digest != expected_sha256.lower():
            raise DeliveryError(
                f"Approved HTML SHA-256 mismatch: expected={expected_sha256.lower()} "
                f"actual={digest}"
            )
    return html_content, digest


def build_unified_html(project_root=None, radar_report=None, effective_date=None):
    root = Path(project_root or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[1])
    quant_html = root / "reports" / "screening_results.html"
    radar_report = radar_report or get_latest_radar_report(
        effective_date=effective_date
    )

    radar_html = ""
    if radar_report and Path(radar_report).exists():
        radar_md = Path(radar_report).read_text(encoding="utf-8")
        radar_md = re.sub(r"!\[.*?\]\(.*?\)", "", radar_md)
        rendered = markdown.markdown(radar_md, extensions=["tables", "md_in_html"])
        radar_html = (
            "<h2>🌍 第一部分：全球前沿产业雷达</h2>\n"
            f"<div style='margin-bottom: 40px;'>{rendered}</div>\n<hr>\n"
        )

    if quant_html.exists():
        html_content = quant_html.read_text(encoding="utf-8")
        if radar_html:
            html_content = html_content.replace(
                "<h1>每日全球策略量化报告</h1>",
                f"<h1>每日全球策略量化报告</h1>\n{radar_html}",
            )
        return html_content
    return (
        "<html><body><div class='container'>"
        "<h1>每日全球策略量化报告</h1>"
        f"{radar_html}<p>No quant report found.</p>"
        "</div></body></html>"
    )


def _load_existing_journal(path: Path, content_hash: str):
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeliveryError(f"Unreadable delivery journal: {path}") from error
    if payload.get("content_sha256") != content_hash:
        raise DeliveryError("Run ID was already used for different email content")
    state = payload.get("state")
    # ``delivered`` is the legacy name for an SMTP-accepted message.  Keep it
    # terminal for idempotency, but never emit it for a new send.
    if state in {"sink", "accepted_by_smtp", "confirmed_received", "delivered"}:
        return payload
    if state == "sending":
        raise DeliveryError(
            "Previous live delivery stopped in ambiguous 'sending' state; "
            "reconcile with SMTP provider before retrying"
        )
    return None


def reconcile_confirmed_delivery(
    *,
    artifact_dir,
    run_id: str,
    expected_html_sha256: str,
    expected_recipient: str,
    confirmed_by: str,
):
    """Record actual receipt only after the recipient confirms it."""
    if not run_id or not RUN_ID_PATTERN.fullmatch(run_id):
        raise DeliveryError(
            "RUN_ID must contain 1-128 safe alphanumeric/._:- characters"
        )
    if not SHA256_PATTERN.fullmatch(expected_html_sha256 or ""):
        raise DeliveryError("Expected HTML SHA-256 must contain exactly 64 hex digits")
    if not expected_recipient:
        raise DeliveryError("Expected recipient is required for delivery reconciliation")
    if confirmed_by != "recipient":
        raise DeliveryError("Delivery reconciliation requires recipient confirmation")

    output_dir = Path(artifact_dir).expanduser().resolve() / "delivery"
    journal_path = output_dir / f"{run_id}.json"
    lock_path = output_dir / f"{run_id}.lock"
    with _delivery_lock(lock_path):
        try:
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise DeliveryError(f"Delivery journal does not exist: {journal_path}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise DeliveryError(f"Unreadable delivery journal: {journal_path}") from error

        actual_hash = str(payload.get("html_sha256") or "").lower()
        if actual_hash != expected_html_sha256.lower():
            raise DeliveryError(
                "Delivery reconciliation HTML SHA-256 mismatch: "
                f"expected={expected_html_sha256.lower()} actual={actual_hash}"
            )
        actual_recipient = payload.get("recipient")
        if actual_recipient != expected_recipient:
            raise DeliveryError(
                "Delivery reconciliation recipient mismatch: "
                f"expected={expected_recipient!r} actual={actual_recipient!r}"
            )
        state = payload.get("state")
        if state in {"confirmed_received", "delivered"}:
            return {**payload, "duplicate": True}
        if state not in {"sending", "accepted_by_smtp"}:
            raise DeliveryError(
                "Only a 'sending' or 'accepted_by_smtp' journal can be "
                "reconciled as confirmed received; "
                f"actual state={state!r}"
            )

        reconciled = {
            **payload,
            "state": "confirmed_received",
            "safe_to_retry": False,
            "reconciled_from": state,
            "reconciliation": "recipient_confirmed_received",
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
            "confirmed_by": confirmed_by,
        }
        _write_journal(journal_path, reconciled)
        return reconciled


def _build_live_message(*, html_content, subject, sender, recipient, run_id):
    inline_images = {}

    def replace_data_image(match):
        subtype = match.group("subtype").lower()
        encoded = re.sub(r"\s+", "", match.group("payload"))
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise DeliveryError("Report contains an invalid base64 data image") from error
        if not image_bytes:
            raise DeliveryError("Report contains an empty base64 data image")
        digest = hashlib.sha256(image_bytes).hexdigest()
        cid = f"gmr-{digest[:24]}"
        inline_images.setdefault(cid, (subtype, image_bytes))
        return f"cid:{cid}"

    rendered_html = DATA_IMAGE_PATTERN.sub(replace_data_image, html_content)
    message = MIMEMultipart("related")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    sender_domain = sender.rpartition("@")[2] or "localhost"
    message_identity = hashlib.sha256(
        (run_id + "\0" + sender + "\0" + html_content).encode("utf-8")
    ).hexdigest()
    message["Message-ID"] = f"<gmr-{message_identity[:32]}@{sender_domain}>"
    message["X-Global-Macro-Radar-Run-ID"] = run_id

    alternatives = MIMEMultipart("alternative")
    alternatives.attach(
        MIMEText(
            "Global Macro Radar report. Please view this message in an HTML-capable mail client.",
            "plain",
            "utf-8",
        )
    )
    alternatives.attach(MIMEText(rendered_html, "html", "utf-8"))
    message.attach(alternatives)

    for index, (cid, (subtype, image_bytes)) in enumerate(
        inline_images.items(), start=1
    ):
        image = MIMEImage(image_bytes, _subtype=subtype)
        image.add_header("Content-ID", f"<{cid}>")
        extension = "svg" if subtype == "svg+xml" else subtype
        image.add_header(
            "Content-Disposition", "inline", filename=f"chart-{index}.{extension}"
        )
        message.attach(image)
    return message, len(inline_images)


def _serialise_refused_recipients(refused):
    """Convert smtplib's refusal mapping into stable JSON-safe evidence."""
    result = {}
    for address, detail in (refused or {}).items():
        try:
            code, response = detail
        except (TypeError, ValueError):
            code, response = None, detail
        if isinstance(response, bytes):
            response = response.decode("utf-8", errors="replace")
        result[str(address)] = {
            "code": int(code) if code is not None else None,
            "message": str(response),
        }
    return result


def deliver_report(
    *,
    html_content: str,
    run_id: str,
    mode: str,
    artifact_dir,
    delivery_config=None,
    smtp_factory=smtplib.SMTP,
    password=None,
    confirm_live_delivery=False,
    report_html_path=None,
):
    if mode not in VALID_DELIVERY_MODES:
        raise DeliveryError(f"Unsupported delivery mode: {mode!r}")
    if mode == "disabled":
        return {"state": "disabled", "run_id": run_id}
    if not run_id or not RUN_ID_PATTERN.fullmatch(run_id):
        raise DeliveryError(
            "RUN_ID/PIPELINE_RUN_ID must contain 1-128 safe alphanumeric/._:- "
            "characters for sink and live delivery"
        )

    delivery_config = delivery_config or {}
    if delivery_config.get("enabled") is False:
        return {"state": "disabled", "run_id": run_id, "reason": "config-disabled"}
    if mode == "live" and not confirm_live_delivery:
        raise DeliveryError("Live delivery requires explicit confirmation")
    subject = delivery_config.get("subject", "🌍 全球前沿产业与量化实盘通讯")
    sender = delivery_config.get("sender_email")
    recipient = delivery_config.get("recipient_email")
    identity = json.dumps(
        {"html": html_content, "subject": subject, "sender": sender, "recipient": recipient},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    html_hash = hashlib.sha256(html_content.encode("utf-8")).hexdigest()
    output_dir = Path(artifact_dir).expanduser().resolve() / "delivery"
    journal_path = output_dir / f"{run_id}.json"
    lock_path = output_dir / f"{run_id}.lock"
    with _delivery_lock(lock_path):
        existing = _load_existing_journal(journal_path, content_hash)
        if existing is not None:
            return {**existing, "duplicate": True}

        base = {
            "schema_version": DELIVERY_JOURNAL_SCHEMA_VERSION,
            "run_id": run_id,
            "mode": mode,
            "content_sha256": content_hash,
            "html_sha256": html_hash,
            "subject": subject,
            "sender": sender,
            "recipient": recipient,
        }
        if mode == "sink":
            html_path = output_dir / f"{run_id}.html"
            _atomic_write(html_path, html_content)
            payload = {**base, "state": "sink", "html_path": str(html_path)}
            _write_journal(journal_path, payload)
            return payload

        if not sender or not recipient:
            raise DeliveryError("Live delivery requires sender_email and recipient_email")
        password = password if password is not None else (
            os.environ.get("SMTP_APP_PASSWORD")
            or os.environ.get("ICLOUD_APP_PASSWORD")
        )
        if not password:
            raise DeliveryError(
                "Live delivery requires SMTP_APP_PASSWORD "
                "(or legacy ICLOUD_APP_PASSWORD)"
            )

        server = delivery_config.get("smtp_server", "smtp.mail.me.com")
        port = int(delivery_config.get("smtp_port", 587))
        smtp_username = (
            delivery_config.get("smtp_username")
            or os.environ.get("SMTP_USERNAME")
            or sender
        )
        message, inline_image_count = _build_live_message(
            html_content=html_content,
            subject=subject,
            sender=sender,
            recipient=recipient,
            run_id=run_id,
        )
        base = {
            **base,
            "inline_image_count": inline_image_count,
            "message_id": message["Message-ID"],
            "message_date": message["Date"],
            "smtp_server": server,
            "smtp_port": port,
            "smtp_username": smtp_username,
        }
        _write_journal(journal_path, {**base, "state": "pending"})

        smtp = None
        try:
            smtp = smtp_factory(server, port, timeout=20)
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(smtp_username, password)
        except Exception as error:
            _write_journal(
                journal_path,
                {
                    **base,
                    "state": "failed_pre_send",
                    "safe_to_retry": True,
                    "error_type": type(error).__name__,
                },
            )
            if smtp is not None and hasattr(smtp, "close"):
                smtp.close()
            raise DeliveryError(f"Live SMTP setup failed: {error}") from error

        _write_journal(journal_path, {**base, "state": "sending"})
        try:
            refused = smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused as error:
            refused = _serialise_refused_recipients(error.recipients)
            payload = {
                **base,
                "state": "rejected_by_smtp",
                "safe_to_retry": True,
                "smtp_refused_recipients": refused,
            }
            _write_journal(journal_path, payload)
            try:
                smtp.quit()
            except Exception:
                if hasattr(smtp, "close"):
                    smtp.close()
            raise DeliveryError(
                "Live SMTP recipient refused: "
                + ", ".join(sorted(refused))
            ) from error
        except Exception as error:
            # Keep the ambiguous state.  Retrying automatically could duplicate mail.
            raise DeliveryError(f"Live SMTP delivery failed: {error}") from error

        refused = _serialise_refused_recipients(refused)
        if refused:
            payload = {
                **base,
                "state": "rejected_by_smtp",
                "safe_to_retry": True,
                "smtp_refused_recipients": refused,
            }
            _write_journal(journal_path, payload)
            try:
                smtp.quit()
            except Exception:
                if hasattr(smtp, "close"):
                    smtp.close()
            raise DeliveryError(
                "Live SMTP recipient refused: "
                + ", ".join(sorted(refused))
            )

        payload = {
            **base,
            "state": "accepted_by_smtp",
            "safe_to_retry": False,
            "smtp_acceptance": "send_message_returned_no_refusals",
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_journal(journal_path, payload)
        try:
            smtp.quit()
        except Exception:
            # The server has already accepted the message.  A QUIT failure must not
            # turn a confirmed delivery into an ambiguous retry candidate.
            if hasattr(smtp, "close"):
                smtp.close()
        return payload


def build_parser():
    parser = argparse.ArgumentParser(description="Deliver unified report")
    parser.add_argument("--mode", choices=sorted(VALID_DELIVERY_MODES))
    parser.add_argument("--run-id")
    parser.add_argument("--artifact-dir")
    parser.add_argument("--env-file")
    parser.add_argument("--html-file")
    parser.add_argument("--prepared-manifest")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--prepared-html")
    parser.add_argument("--expected-html-sha256")
    parser.add_argument(
        "--effective-date",
        help="Logical report date in YYYY-MM-DD; defaults to the pipeline environment",
    )
    parser.add_argument(
        "--confirm-live-delivery",
        action="store_true",
        help="Required explicit acknowledgement for live SMTP delivery",
    )
    parser.add_argument(
        "--reconcile-confirmed-delivery",
        action="store_true",
        help=(
            "Mark a sending/accepted_by_smtp journal confirmed_received after "
            "recipient confirmation"
        ),
    )
    parser.add_argument("--expected-recipient")
    parser.add_argument(
        "--confirm-recipient-received",
        action="store_true",
        help="Required acknowledgement that the recipient confirmed receipt",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    _load_runtime_environment(args.env_file)
    project_root = Path(
        os.environ.get("PROJECT_ROOT")
        or Path(__file__).resolve().parents[1]
    )
    report_html_path = project_root / "reports" / "screening_results.html"
    if args.prepare_only:
        if not args.prepared_html or not args.prepared_manifest:
            raise DeliveryError(
                "--prepare-only requires --prepared-html and --prepared-manifest"
            )
        html_content = build_unified_html(
            project_root=project_root,
            effective_date=args.effective_date,
        )
        prepared_path = Path(args.prepared_html).expanduser().resolve()
        manifest_path = Path(args.prepared_manifest).expanduser().resolve()
        digest = hashlib.sha256(html_content.encode("utf-8")).hexdigest()
        _atomic_write(prepared_path, html_content)
        _atomic_write(report_html_path, html_content)
        manifest = {
            "schema_version": 1,
            "html_path": str(prepared_path),
            "canonical_report_path": str(report_html_path.resolve()),
            "html_sha256": digest,
        }
        _atomic_write(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
        )
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        return 0

    mode = args.mode or os.environ.get("DELIVERY_MODE", "disabled")
    run_id = args.run_id or os.environ.get("PIPELINE_RUN_ID") or os.environ.get("RUN_ID")
    artifact_dir = (
        args.artifact_dir
        or os.environ.get("PIPELINE_ARTIFACT_DIR")
        or os.environ.get("ARTIFACT_DIR")
    )
    if mode != "disabled" and not artifact_dir:
        raise DeliveryError("ARTIFACT_DIR/PIPELINE_ARTIFACT_DIR is required")
    if args.reconcile_confirmed_delivery:
        if not artifact_dir:
            raise DeliveryError("ARTIFACT_DIR/PIPELINE_ARTIFACT_DIR is required")
        if not args.confirm_recipient_received:
            raise DeliveryError(
                "Delivery reconciliation requires --confirm-recipient-received"
            )
        result = reconcile_confirmed_delivery(
            artifact_dir=artifact_dir,
            run_id=run_id or "",
            expected_html_sha256=args.expected_html_sha256 or "",
            expected_recipient=args.expected_recipient or "",
            confirmed_by="recipient",
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.expected_html_sha256 and not args.html_file:
        raise DeliveryError("--expected-html-sha256 requires --html-file")
    if args.prepared_manifest and args.html_file:
        raise DeliveryError("--prepared-manifest and --html-file are mutually exclusive")
    if mode == "live" and args.html_file and not args.expected_html_sha256:
        raise DeliveryError(
            "Live delivery of an approved HTML file requires --expected-html-sha256"
        )
    if args.prepared_manifest:
        manifest_path = Path(args.prepared_manifest).expanduser().resolve()
        try:
            prepared = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DeliveryError(
                f"Unable to read prepared HTML manifest: {manifest_path}"
            ) from error
        if prepared.get("schema_version") != 1:
            raise DeliveryError("Unsupported prepared HTML manifest schema")
        html_content, _ = load_approved_html(
            prepared.get("html_path", ""),
            expected_sha256=prepared.get("html_sha256"),
        )
    elif args.html_file:
        html_content, _ = load_approved_html(
            args.html_file, expected_sha256=args.expected_html_sha256
        )
    else:
        html_content = build_unified_html(effective_date=args.effective_date)
    result = deliver_report(
        html_content=html_content,
        run_id=run_id or "",
        mode=mode,
        artifact_dir=artifact_dir or ".",
        delivery_config=_load_delivery_config(),
        confirm_live_delivery=args.confirm_live_delivery,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeliveryError as error:
        print(f"Delivery failed: {error}", file=sys.stderr)
        raise SystemExit(1)
