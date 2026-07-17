"""Build and deliver the unified report through an explicit delivery mode.

Non-production runs use the local sink.  Live delivery is journaled by run ID;
an interrupted ``sending`` state is deliberately ambiguous and therefore
requires operator reconciliation rather than risking a duplicate message.
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
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import markdown
import yaml
from dotenv import load_dotenv


VALID_DELIVERY_MODES = {"disabled", "sink", "live"}
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


def get_latest_radar_report(reports_dir=None):
    default_reports_dir = Path(__file__).resolve().parents[2] / "industry-radar" / "reports"
    directory = Path(
        reports_dir or os.environ.get("RADAR_REPORTS_DIR", default_reports_dir)
    )
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


def build_unified_html(project_root=None, radar_report=None):
    root = Path(project_root or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[1])
    quant_html = root / "reports" / "screening_results.html"
    radar_report = radar_report or get_latest_radar_report()

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
    if state in {"sink", "delivered"}:
        return payload
    if state == "sending":
        raise DeliveryError(
            "Previous live delivery stopped in ambiguous 'sending' state; "
            "reconcile with SMTP provider before retrying"
        )
    return None


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
            "schema_version": 1,
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
        password = (
            password
            if password is not None
            else os.environ.get("ICLOUD_APP_PASSWORD")
        )
        if not password:
            raise DeliveryError("Live delivery requires ICLOUD_APP_PASSWORD")

        server = delivery_config.get("smtp_server", "smtp.mail.me.com")
        port = int(delivery_config.get("smtp_port", 587))
        message, inline_image_count = _build_live_message(
            html_content=html_content,
            subject=subject,
            sender=sender,
            recipient=recipient,
            run_id=run_id,
        )
        base = {**base, "inline_image_count": inline_image_count}
        _write_journal(journal_path, {**base, "state": "pending"})

        smtp = None
        try:
            smtp = smtp_factory(server, port, timeout=20)
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(sender, password)
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
            smtp.send_message(message)
        except Exception as error:
            # Keep the ambiguous state.  Retrying automatically could duplicate mail.
            raise DeliveryError(f"Live SMTP delivery failed: {error}") from error

        payload = {**base, "state": "delivered"}
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
    parser.add_argument("--expected-html-sha256")
    parser.add_argument(
        "--confirm-live-delivery",
        action="store_true",
        help="Required explicit acknowledgement for live SMTP delivery",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    _load_runtime_environment(args.env_file)
    mode = args.mode or os.environ.get("DELIVERY_MODE", "disabled")
    run_id = args.run_id or os.environ.get("PIPELINE_RUN_ID") or os.environ.get("RUN_ID")
    artifact_dir = (
        args.artifact_dir
        or os.environ.get("PIPELINE_ARTIFACT_DIR")
        or os.environ.get("ARTIFACT_DIR")
    )
    if mode != "disabled" and not artifact_dir:
        raise DeliveryError("ARTIFACT_DIR/PIPELINE_ARTIFACT_DIR is required")
    if args.expected_html_sha256 and not args.html_file:
        raise DeliveryError("--expected-html-sha256 requires --html-file")
    if mode == "live" and args.html_file and not args.expected_html_sha256:
        raise DeliveryError(
            "Live delivery of an approved HTML file requires --expected-html-sha256"
        )
    if args.html_file:
        html_content, _ = load_approved_html(
            args.html_file, expected_sha256=args.expected_html_sha256
        )
    else:
        html_content = build_unified_html()
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
