"""Build and deliver the unified report through an explicit delivery mode.

Non-production runs use the local sink.  Live delivery is journaled by run ID;
an interrupted ``sending`` state is deliberately ambiguous and therefore
requires operator reconciliation rather than risking a duplicate message.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import smtplib
import sys
import tempfile
from contextlib import contextmanager
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import markdown
import yaml
from dotenv import load_dotenv


VALID_DELIVERY_MODES = {"disabled", "sink", "live"}
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


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


def deliver_report(
    *,
    html_content: str,
    run_id: str,
    mode: str,
    artifact_dir,
    delivery_config=None,
    smtp_factory=smtplib.SMTP,
    password=None,
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
        _write_journal(journal_path, {**base, "state": "pending"})

        message = MIMEMultipart("related")
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = recipient
        message.attach(MIMEText(html_content, "html", "utf-8"))

        _write_journal(journal_path, {**base, "state": "sending"})
        try:
            smtp = smtp_factory(server, port, timeout=20)
            smtp.starttls()
            smtp.login(sender, password)
            smtp.send_message(message)
            smtp.quit()
        except Exception as error:
            # Keep the ambiguous state.  Retrying automatically could duplicate mail.
            raise DeliveryError(f"Live SMTP delivery failed: {error}") from error

        payload = {**base, "state": "delivered"}
        _write_journal(journal_path, payload)
        return payload


def build_parser():
    parser = argparse.ArgumentParser(description="Deliver unified report")
    parser.add_argument("--mode", choices=sorted(VALID_DELIVERY_MODES))
    parser.add_argument("--run-id")
    parser.add_argument("--artifact-dir")
    return parser


def main(argv=None):
    env_path = os.environ.get("RADAR_ENV")
    if env_path and Path(env_path).exists():
        load_dotenv(env_path)
    args = build_parser().parse_args(argv)
    mode = args.mode or os.environ.get("DELIVERY_MODE", "disabled")
    run_id = args.run_id or os.environ.get("PIPELINE_RUN_ID") or os.environ.get("RUN_ID")
    artifact_dir = (
        args.artifact_dir
        or os.environ.get("PIPELINE_ARTIFACT_DIR")
        or os.environ.get("ARTIFACT_DIR")
    )
    if mode != "disabled" and not artifact_dir:
        raise DeliveryError("ARTIFACT_DIR/PIPELINE_ARTIFACT_DIR is required")
    result = deliver_report(
        html_content=build_unified_html(),
        run_id=run_id or "",
        mode=mode,
        artifact_dir=artifact_dir or ".",
        delivery_config=_load_delivery_config(),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeliveryError as error:
        print(f"Delivery failed: {error}", file=sys.stderr)
        raise SystemExit(1)
