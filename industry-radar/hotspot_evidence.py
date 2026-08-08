"""Publish the report-to-trading hotspot evidence boundary.

The Markdown report is presentation output.  This module emits a small,
machine-readable artifact that is bound to the exact report bytes and only
contains events that passed the deterministic first-party evidence policy.
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


HOTSPOT_EVIDENCE_SCHEMA_VERSION = 1


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_articles(articles):
    unique = []
    seen = set()
    for article in articles:
        identity = str(article.get("link") or article.get("title") or "")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        unique.append(article)
    return unique


def _event(article):
    score = article.get("score_data") or {}
    link = str(article.get("link") or "")
    return {
        "event_id": hashlib.sha256(link.encode("utf-8")).hexdigest(),
        "title": str(article.get("title") or ""),
        "summary": str(article.get("summary") or article.get("content") or ""),
        "link": link,
        "source": str(article.get("source") or ""),
        "source_id": str(article.get("source_id") or ""),
        "source_tier": str(article.get("source_tier") or ""),
        "evidence_state": str(article.get("evidence_state") or ""),
        "trade_evidence_eligible": True,
        "event_type": str(score.get("event_type") or ""),
        "innovation_score": float(score.get("innovation_score") or 0),
        "traffic_score": float(score.get("traffic_score") or 0),
        "strategic_topic": str(article.get("strategic_topic") or "unrelated"),
        "industrial_milestone": str(
            article.get("industrial_milestone") or "none"
        ),
        "production_state": str(article.get("production_state") or "none"),
    }


def publish_hotspot_evidence(report_path, articles, effective_date):
    """Atomically publish trade-eligible events bound to ``report_path``."""
    report = Path(report_path).resolve()
    if not report.is_file():
        raise FileNotFoundError(f"industry report does not exist: {report}")

    selected = _unique_articles(articles)
    trade_articles = [
        article
        for article in selected
        if article.get("trade_evidence_eligible") is True
    ]
    payload = {
        "schema_version": HOTSPOT_EVIDENCE_SCHEMA_VERSION,
        "effective_date": str(effective_date),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision_state": "replace_targets" if trade_articles else "no_change",
        "source_report": {
            "path": str(report),
            "sha256": _sha256_file(report),
        },
        "events": [_event(article) for article in trade_articles],
        "research_event_count": len(selected) - len(trade_articles),
    }

    destination = report.parent / f"hotspot_evidence_{effective_date}.json"
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=".hotspot_evidence.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return str(destination)
