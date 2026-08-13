"""Publish the report-to-trading hotspot evidence boundary.

The Markdown report is presentation output.  This module emits a small,
machine-readable artifact that is bound to the exact report bytes and only
contains events that passed the deterministic first-party evidence policy.
"""

import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


HOTSPOT_EVIDENCE_SCHEMA_VERSION = 2


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


def _canonical_article_projection(article):
    score = article.get("score_data") or {}
    corroboration = article.get("primary_corroboration") or {}
    return {
        "link": str(article.get("link") or ""),
        "title": " ".join(str(article.get("title") or "").split()),
        "source_id": str(article.get("source_id") or ""),
        "source_tier": str(article.get("source_tier") or ""),
        "event_type": str(score.get("event_type") or ""),
        "innovation_score": float(score.get("innovation_score") or 0),
        "traffic_score": float(score.get("traffic_score") or 0),
        "industrial_milestone": str(
            article.get("industrial_milestone") or "none"
        ),
        "production_state": str(article.get("production_state") or "none"),
        "trade_evidence_eligible": article.get("trade_evidence_eligible") is True,
        "trade_evidence_reason": str(
            (article.get("trade_evidence_decision") or {}).get("reason") or ""
        ),
        "corroboration_method": str(corroboration.get("method") or ""),
        "corroboration_url": str(corroboration.get("primary_url") or ""),
        "event_cluster_id": str(corroboration.get("event_cluster_id") or ""),
        "event_cluster_version": str(
            corroboration.get("event_cluster_version") or ""
        ),
        "event_product_name": str(
            corroboration.get("event_product_name") or ""
        ),
    }


def _canonical_articles_sha256(articles):
    projections = [_canonical_article_projection(article) for article in articles]
    projections.sort(
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    encoded = json.dumps(
        projections,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_sha256(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rejected_event(article):
    """Return a stable, minimal audit record for a rejected event.

    The full article remains outside the trading boundary.  This projection is
    deliberately limited to identity and policy-decision fields so operators
    can explain a zero-event run without archiving mutable article bodies.
    """
    title = " ".join(str(article.get("title") or "").split())
    identity = str(article.get("link") or title)
    record = {
        "event_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "title": title,
        "source_id": str(article.get("source_id") or ""),
        "source_tier": str(article.get("source_tier") or ""),
        "reason": str(
            (article.get("trade_evidence_decision") or {}).get("reason")
            or "missing_trade_evidence_decision"
        ),
    }
    return {**record, "decision_sha256": _canonical_json_sha256(record)}


def _source_evidence_text(article):
    """Preserve first-party body text needed to bind an issuer to an event."""
    parts = []
    seen = set()
    for field in ("summary", "content"):
        value = " ".join(str(article.get(field) or "").split())
        normalized = value.casefold()
        if value and normalized not in seen:
            seen.add(normalized)
            parts.append(value)
    return "\n".join(parts)[:20000]


def _event(article):
    score = article.get("score_data") or {}
    decision = article.get("trade_evidence_decision") or {}
    corroboration = article.get("primary_corroboration") or {}
    if not isinstance(corroboration, dict):
        corroboration = {}
    link = str(article.get("link") or "")
    return {
        "event_id": hashlib.sha256(link.encode("utf-8")).hexdigest(),
        "event_cluster_id": str(corroboration.get("event_cluster_id") or ""),
        "title": str(article.get("title") or ""),
        "summary": str(article.get("summary") or article.get("content") or ""),
        "evidence_text": _source_evidence_text(article),
        "link": link,
        "source": str(article.get("source") or ""),
        "source_id": str(article.get("source_id") or ""),
        "source_tier": str(article.get("source_tier") or ""),
        "evidence_state": str(article.get("evidence_state") or ""),
        "trade_evidence_eligible": True,
        "trade_evidence_reason": str(
            decision.get("reason") or ""
        ),
        "requires_direct_entity_binding": (
            decision.get("requires_direct_entity_binding") is True
        ),
        "event_type": str(score.get("event_type") or ""),
        "innovation_score": float(score.get("innovation_score") or 0),
        "traffic_score": float(score.get("traffic_score") or 0),
        "strategic_topic": str(article.get("strategic_topic") or "unrelated"),
        "industrial_milestone": str(
            article.get("industrial_milestone") or "none"
        ),
        "production_state": str(article.get("production_state") or "none"),
        "primary_corroboration": dict(corroboration),
    }


def publish_hotspot_evidence(
    report_path,
    articles,
    effective_date,
    *,
    eligible_input_articles=None,
    report_selected_count=None,
):
    """Atomically publish trade-eligible events bound to ``report_path``."""
    report = Path(report_path).resolve()
    if not report.is_file():
        raise FileNotFoundError(f"industry report does not exist: {report}")

    selected = _unique_articles(articles)
    eligible_input = list(
        articles if eligible_input_articles is None else eligible_input_articles
    )
    trade_articles = [
        article
        for article in selected
        if article.get("trade_evidence_eligible") is True
    ]
    rejection_reason_counts = Counter(
        str(
            (article.get("trade_evidence_decision") or {}).get("reason")
            or "missing_trade_evidence_decision"
        )
        for article in selected
        if article.get("trade_evidence_eligible") is not True
    )
    rejected_events = [
        _rejected_event(article)
        for article in selected
        if article.get("trade_evidence_eligible") is not True
    ]
    rejected_events.sort(
        key=lambda item: (item["event_id"], item["decision_sha256"])
    )
    evidence_evaluated_count = len(selected)
    if report_selected_count is None:
        # Direct callers written before the explicit presentation count used
        # the evaluated set as their closest available approximation.
        report_selected_count = evidence_evaluated_count
    if type(report_selected_count) is not int or report_selected_count < 0:
        raise ValueError("report_selected_count must be a non-negative integer")
    payload = {
        "schema_version": HOTSPOT_EVIDENCE_SCHEMA_VERSION,
        "effective_date": str(effective_date),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision_state": "replace_targets" if trade_articles else "no_change",
        "source_report": {
            "path": str(report),
            "sha256": _sha256_file(report),
        },
        "eligible_input_sha256": _canonical_articles_sha256(eligible_input),
        "selection_sha256": _canonical_articles_sha256(selected),
        "events": [_event(article) for article in trade_articles],
        "evidence_input_count": len(_unique_articles(eligible_input)),
        # selected_article_count is retained as a compatibility alias.  It has
        # always counted the evidence-policy evaluation set, not report rows.
        "selected_article_count": evidence_evaluated_count,
        "evidence_evaluated_count": evidence_evaluated_count,
        "report_selected_count": report_selected_count,
        "research_event_count": evidence_evaluated_count - len(trade_articles),
        "trade_event_count": len(trade_articles),
        "trade_evidence_rejections": rejected_events,
        "trade_evidence_rejections_sha256": _canonical_json_sha256(
            rejected_events
        ),
        "trade_evidence_rejection_reasons": dict(
            sorted(rejection_reason_counts.items())
        ),
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
