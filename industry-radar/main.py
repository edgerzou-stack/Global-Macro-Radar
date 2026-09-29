import yaml
import os
import json
import logging
import tempfile
import hashlib
from pathlib import Path
from datetime import date, timedelta
from score import score_article
from cache_manager import load_cache
from dotenv import load_dotenv
from pipeline_deep_dive import (
    emit_pipeline_metric,
    enrich_deep_dives,
    run_deep_dive_job,
)
from pipeline_health import (
    aware_utc_timestamp as _aware_utc_timestamp,
    rss_reference_time_utc,
    validate_rss_fixture_effective_date,
    validate_rss_health,
)
from pipeline_delivery import send_email
from pipeline_ingestion import collect_articles
from pipeline_rendering import generate_markdown_report
from pipeline_scoring import (
    configured_scoring_identities,
    find_cached_article,
    load_scored_articles_fixture,
    llm_calls_disabled,
    run_validated_batch,
    score_articles_pipeline,
    scoring_cache_config,
    store_article_score,
    validate_scoring_configuration,
)
from llm_cost_policy import (
    resolve_policy,
    start_run,
    write_interactive_rss_fixture,
    write_telemetry,
)
from pipeline_selection import is_verified_deep_dive, review_is_pending
from provider_errors import log_provider_error
from ingest import fetch_rss_feeds, load_rss_fixture
from run_date import logical_date_text
from url_identity import canonicalize_article_url


logger = logging.getLogger(__name__)


def save_json_atomic(path, payload):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
        ) as handle:
            temporary_path = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _canonical_sha256(payload):
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_audit_entry(article):
    score = article.get("score_data") or {}
    entry = {
        "link": str(article.get("link") or ""),
        "title": str(article.get("title") or ""),
        "source": str(article.get("source") or ""),
        "source_id": str(article.get("source_id") or ""),
        "source_tier": str(article.get("source_tier") or ""),
        "source_lane": str(article.get("source_lane") or ""),
        "published_at": str(article.get("published_at") or ""),
        "evidence_state": str(article.get("evidence_state") or ""),
        "is_relevant": score.get("is_relevant") is True,
        "event_type": str(score.get("event_type") or ""),
        "innovation_score": score.get("innovation_score", 0),
        "traffic_score": score.get("traffic_score", 0),
        "prompt_version": str(score.get("prompt_version") or ""),
        "llm_provider": str(score.get("llm_provider") or ""),
        "llm_model": str(score.get("llm_model") or ""),
    }
    if article.get("review_outcome") == "unscored_priority":
        entry.update(
            {
                "review_outcome": "unscored_priority",
                "priority_display": dict(article.get("priority_display") or {}),
                "priority_reason": str(article.get("_priority_reason") or ""),
            }
        )
    return entry


def write_news_selection_audit(reports_dir, ingestion, scored_articles, config):
    """Account for every fetched item without copying article bodies into audit."""
    selection_path = os.path.join(reports_dir, "radar_selection_health.json")
    with open(selection_path, "r", encoding="utf-8") as handle:
        selection = json.load(handle)
    decisions = {
        canonicalize_article_url(item.get("link") or ""): item
        for item in selection.get("selection_decisions", [])
    }
    decisions.update({
        canonicalize_article_url(item.get("link") or ""): item
        for item in selection.get("priority_selection_decisions", [])
    })
    scored = {
        canonicalize_article_url(item.get("link") or ""): item
        for item in scored_articles
    }
    rows = []

    def row_for(article, *, representative_link=None):
        link = canonicalize_article_url(article.get("link") or "")
        score_article = scored.get(link, article)
        score = score_article.get("score_data") or {}
        decision = decisions.get(link)
        priority = score_article.get("review_outcome") == "unscored_priority"
        if representative_link is not None:
            status, reason, lane = "duplicate", "duplicate_of_input", "not_rendered"
        elif decision is not None:
            representative_link = (
                representative_link or decision.get("representative_link")
            )
            lane = decision.get("report_lane") or "not_rendered"
            status = "selected" if decision.get("rendered") else "filtered"
            reason = decision.get("reason") or "unknown_selection_reason"
        elif priority:
            status, reason, lane = "pending", "missing_priority_selection_decision", "not_rendered"
        elif score_article.get("_score_resolution") == "unscored_priority_filtered":
            status, reason, lane = (
                "filtered", score_article.get("_priority_reason") or "priority_industry_review_failed", "not_rendered"
            )
        elif review_is_pending(score_article):
            status, reason, lane = "pending", "review_not_completed", "not_rendered"
        elif score.get("is_relevant") is False:
            status, reason, lane = "filtered", score.get("justification") or "not_industry_relevant", "not_rendered"
        elif score.get("is_relevant") is True and article.get("published_at"):
            day = str(article["published_at"])[:10]
            effective = date.fromisoformat(logical_date_text())
            lookback = int(config.get("output", {}).get("report_days_lookback", 2))
            if not ((effective - timedelta(days=lookback)).isoformat() <= day <= effective.isoformat()):
                status, reason, lane = "filtered", "outside_effective_window", "not_rendered"
            else:
                status, reason, lane = "pending", "missing_terminal_decision", "not_rendered"
        else:
            status, reason, lane = "pending", "missing_terminal_decision", "not_rendered"
        material = {key: article.get(key) for key in ("title", "summary", "content", "link", "published_at")}
        return {
            "link": link,
            "raw_link": str(article.get("link") or ""),
            "title": str(article.get("title") or ""),
            "source": str(article.get("source") or article.get("source_id") or ""),
            "source_id": str(article.get("source_id") or ""),
            "published_at": str(article.get("published_at") or ""),
            "source_material_sha256": _canonical_sha256(material),
            "representative_link": representative_link,
            "decision": status,
            "reason": str(reason),
            "report_lane": lane,
            "review_resolution": str(score_article.get("_score_resolution") or ""),
            "is_relevant": score.get("is_relevant"),
            "event_type": str(score.get("event_type") or ""),
            "industrial_claims": list(score.get("industrial_claims") or []),
            "factual_basis": str((score_article.get("priority_industry_review") or {}).get("factual_basis") or ""),
            "prompt_version": str(score.get("prompt_version") or ""),
            "cache_hit": bool(score_article.get("_score_cache_hit")),
            "chinese_display_complete": bool(
                (score_article.get("priority_display") or score).get("translated_title")
                and (score_article.get("priority_display") or score).get("translated_summary")
            ),
        }

    for article in ingestion.articles:
        rows.append(row_for(article))
    for duplicate in getattr(ingestion, "duplicate_inputs", ()):
        rows.append(row_for(duplicate["article"], representative_link=duplicate["representative_link"]))
    counts = {status: sum(row["decision"] == status for row in rows) for status in ("selected", "filtered", "duplicate", "pending")}
    payload = {
        "schema_version": 1,
        "run_id": os.environ.get("PIPELINE_RUN_ID", "standalone"),
        "effective_date": logical_date_text(),
        "raw_input_count": len(rows),
        "unique_input_count": len(ingestion.articles),
        "decision_counts": counts,
        "rows": rows,
    }
    target = os.path.join(reports_dir, "news_selection_audit.json")
    save_json_atomic(target, payload)
    selection["news_audit_counts"] = counts
    selection["news_audit_raw_input_count"] = len(rows)
    selection["news_audit_unique_input_count"] = len(ingestion.articles)
    save_json_atomic(selection_path, selection)
    reason_zh = {
        "duplicate_of_input": "与已记录文章重复",
        "outside_effective_window": "不在本期时间范围",
        "review_not_completed": "内容审阅未完成",
        "missing_terminal_decision": "缺少最终筛选结论",
        "reviewed_unscored_priority": "已审产业事件，确实无法数值评分，置顶展示",
        "reviewed_not_industry_relevant": "已审查，不是具体产业事件",
        "priority_industry_review_missing": "缺少产业事实审查",
        "priority_industry_review_invalid": "产业事实审查不合格",
        "report_score_below_threshold": "低于报告8分门槛",
        "duplicate_event_report": "同一产业事件的重复报道，保留另一篇代表报道",
        "excluded_by_discovery_source_cap": "同来源限额",
        "eligible_but_section_capacity_exceeded": "栏目容量已满",
    }
    lines = [
        f"# 新闻筛选清单（{logical_date_text()}）",
        "",
        f"原始输入 {len(rows)} 条；独立文章 {len(ingestion.articles)} 条；"
        f"报告入选 {counts['selected']} 条；有依据排除 {counts['filtered']} 条；"
        f"重复 {counts['duplicate']} 条；待处理 {counts['pending']} 条。",
        "",
        "以下原标题保留原文，筛选结论和原因以中文说明；完整结构化记录见 news_selection_audit.json。",
        "",
    ]
    headings = {"selected": "入选", "filtered": "排除", "pending": "待处理", "duplicate": "重复"}
    for status in ("selected", "filtered", "pending", "duplicate"):
        lines.extend([f"## {headings[status]}", ""])
        for row in rows:
            if row["decision"] != status:
                continue
            reason = reason_zh.get(row["reason"], row["reason"])
            representative = (
                f" · [保留报道]({row['representative_link']})"
                if row["reason"] == "duplicate_event_report"
                and row.get("representative_link")
                else ""
            )
            lines.append(
                f"- [{row['title']}]({row['link']}) · {row['source']} · {reason}"
                f"{representative}"
            )
        lines.append("")
    preview_path = Path(reports_dir) / "news_selection_preview.md"
    preview_path.write_text("\n".join(lines), encoding="utf-8")
    policy = resolve_policy(config)
    if policy.api_enabled and policy.complete_review and counts["pending"]:
        raise RuntimeError(f"Complete review left {counts['pending']} news items pending")
    return Path(target)


def write_run_snapshot(
    reports_dir,
    ingestion,
    scored_articles,
    report_path,
    config,
):
    """Persist enough immutable evidence to explain same-day report changes."""
    effective_date = logical_date_text()
    candidates = sorted(
        (_candidate_audit_entry(article) for article in scored_articles),
        key=lambda item: (
            item["published_at"],
            item["link"],
            item["title"],
        ),
    )
    input_identities = sorted(
        (
            {
                "link": str(article.get("link") or ""),
                "title": str(article.get("title") or ""),
                "published_at": str(article.get("published_at") or ""),
                "source_id": str(article.get("source_id") or ""),
            }
            for article in ingestion.articles
        ),
        key=lambda item: (
            item["published_at"],
            item["link"],
            item["title"],
        ),
    )
    selection_path = os.path.join(reports_dir, "radar_selection_health.json")
    with open(selection_path, "r", encoding="utf-8") as handle:
        selection = json.load(handle)
    hotspot_path = os.path.join(
        reports_dir,
        f"hotspot_evidence_{effective_date}.json",
    )
    artifact_paths = {
        os.path.basename(report_path): report_path,
        os.path.basename(hotspot_path): hotspot_path,
        "rss_health.json": os.path.join(reports_dir, "rss_health.json"),
        "rss_health_summary.json": os.path.join(
            reports_dir, "rss_health_summary.json"
        ),
        "radar_selection_health.json": selection_path,
    }
    llm_cost_path = os.path.join(reports_dir, "radar_llm_usage.json")
    if os.path.isfile(llm_cost_path):
        artifact_paths["radar_llm_usage.json"] = llm_cost_path
    audit_path = os.path.join(reports_dir, "news_selection_audit.json")
    if os.path.isfile(audit_path):
        artifact_paths["news_selection_audit.json"] = audit_path
    preview_path = os.path.join(reports_dir, "news_selection_preview.md")
    if os.path.isfile(preview_path):
        artifact_paths["news_selection_preview.md"] = preview_path
    snapshot = {
        "schema_version": 1,
        "component": "radar-run-snapshot",
        "run_id": os.environ.get("PIPELINE_RUN_ID", "standalone"),
        "effective_date": effective_date,
        "capture_mode": "live" if not os.environ.get("RADAR_RSS_FIXTURE") else "fixture",
        "reference_time": ingestion.reference_time.isoformat(),
        "input_article_count": len(input_identities),
        "input_identity_sha256": _canonical_sha256(input_identities),
        "candidate_count": len(candidates),
        "candidate_sha256": _canonical_sha256(candidates),
        "config_sha256": _canonical_sha256(config),
        "report": {
            "path": os.path.abspath(report_path),
            "sha256": _file_sha256(report_path),
        },
        "hotspot_evidence": {
            "path": os.path.abspath(hotspot_path),
            "sha256": _file_sha256(hotspot_path),
        },
        "artifacts": {
            name: {
                "sha256": _file_sha256(path),
                "size_bytes": os.path.getsize(path),
            }
            for name, path in sorted(artifact_paths.items())
        },
        "selection": selection,
        "candidates": candidates,
    }
    replay_digest = os.environ.get("RADAR_CAPTURE_REPLAY_SHA256")
    if replay_digest:
        fixture_path = os.environ.get("RADAR_RSS_FIXTURE")
        if (
            not fixture_path
            or len(replay_digest) != 64
            or any(character not in "0123456789abcdef" for character in replay_digest.lower())
            or _file_sha256(fixture_path) != replay_digest.lower()
        ):
            raise ValueError("radar replay fixture identity is invalid")
        snapshot["replay_rss_fixture_sha256"] = replay_digest.lower()
    target = os.path.join(reports_dir, "radar_run_snapshot.json")
    save_json_atomic(target, snapshot)
    return target


def load_config(config_path=None):
    config_path = config_path or os.environ.get("RADAR_CONFIG", "config.yaml")
    # P4.1: Graceful fallback for missing config.yaml
    if not os.path.exists(config_path):
        example_path = os.path.join(
            os.path.dirname(os.path.abspath(config_path)), "config.example.yaml"
        )
        if not os.path.exists(example_path) and config_path == "config.yaml":
            example_path = "config.example.yaml"
        if os.path.exists(example_path):
            import shutil
            shutil.copy2(example_path, config_path)
            print(f"Warning: {config_path} not found. Auto-created from {example_path}.")
        else:
            raise FileNotFoundError(f"Missing both {config_path} and {example_path}!")
            
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def write_scoring_failure_evidence(reports_dir, ingestion, articles, config, error):
    """Record an incomplete scoring attempt without creating a report snapshot."""

    completed = {
        "ai", "cache", "deterministic", "manual",
        "unscored_priority", "unscored_priority_display_cache",
        "unscored_priority_filtered",
    }
    inputs = []
    for article in articles:
        resolution = article.get("_score_resolution")
        status = (
            resolution
            if resolution in completed and not review_is_pending(article)
            else "pending"
        )
        material = {
            key: article.get(key)
            for key in ("title", "summary", "content", "link", "published_at")
        }
        inputs.append({
            "link": str(article.get("link") or ""),
            "source": str(article.get("source") or article.get("source_id") or ""),
            "source_material_sha256": _canonical_sha256(material),
            "status": status,
        })
    duplicate_inputs = [
        {
            "link": str(entry.get("article", {}).get("link") or ""),
            "representative_link": str(entry.get("representative_link") or ""),
        }
        for entry in getattr(ingestion, "duplicate_inputs", ())
    ]
    completed_count = sum(item["status"] != "pending" for item in inputs)
    save_json_atomic(
        os.path.join(reports_dir, "radar_scoring_failure.json"),
        {
            "schema_version": 1,
            "component": "radar-scoring-failure",
            "status": "failed",
            "run_id": os.environ.get("PIPELINE_RUN_ID", "standalone"),
            "effective_date": logical_date_text(),
            "error_type": type(error).__name__,
            "error_message": str(error),
            "captured_unique_count": len(articles),
            "captured_duplicate_count": len(duplicate_inputs),
            "captured_raw_count": len(articles) + len(duplicate_inputs),
            "completed_score_count": completed_count,
            "review_pending_count": len(articles) - completed_count,
            "validated_api_score_count": sum(
                item["status"] == "ai" for item in inputs
            ),
            "validated_batch_paths": list(
                config.get("_runtime", {}).get(
                    "validated_scoring_batch_paths", []
                )
            ),
            "global_cache_reuse_authorized": False,
            "inputs": inputs,
            "duplicate_inputs": duplicate_inputs,
        },
    )

def main():
    print("Starting Dual-Track Industry Intelligence Gatherer...", flush=True)
    config = load_config()
    policy = resolve_policy(config)
    # Offline regression and interactive folder-AI modes must not even load
    # project API credentials. API modes retain the reviewed DeepSeek/OpenAI
    # provider workflow and load secrets only after the mode contract is known.
    if policy.api_enabled:
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
    reports_dir = os.environ.get("RADAR_REPORTS_DIR", "reports")
    controller = start_run(config)
    
    print("Fetching articles from RSS feeds...", flush=True)
    ingestion = collect_articles(
        config,
        save_health=lambda health: save_json_atomic(
            os.path.join(reports_dir, "rss_health.json"),
            health,
        ),
        load_fixture=load_rss_fixture,
        fetch_feeds=fetch_rss_feeds,
    )
    save_json_atomic(
        os.path.join(reports_dir, "rss_health_summary.json"),
        {
            **ingestion.health_summary,
            "schema_version": 1,
            "run_id": os.environ.get("PIPELINE_RUN_ID", "standalone"),
            "effective_date": logical_date_text(),
            "component": "rss-health-summary",
        },
    )
    articles = list(ingestion.articles)
    print(f"Fetched {len(articles)} articles.", flush=True)
    if ingestion.duplicate_count:
        print(
            "Pre-scoring deduplication removed "
            f"{ingestion.duplicate_count} duplicates. "
            f"{len(articles)} articles remaining.",
            flush=True,
        )

    scored_fixture = os.environ.get("RADAR_SCORED_ARTICLES_FIXTURE")
    if policy.mode == "interactive" and not scored_fixture:
        rss_fixture_path = os.environ.get("RADAR_LLM_RSS_FIXTURE") or os.path.join(
            reports_dir,
            "llm-review-rss-fixture.json",
        )
        sealed = write_interactive_rss_fixture(
            rss_fixture_path,
            articles,
            ingestion.health,
            reference_time=ingestion.reference_time,
            run_id=os.environ.get("PIPELINE_RUN_ID"),
        )
        config.setdefault("_runtime", {})[
            "interactive_rss_fixture_path"
        ] = sealed["path"]
    if scored_fixture:
        print(
            f"Loading deterministic scored-articles fixture: {scored_fixture}",
            flush=True,
        )
        scored_articles = load_scored_articles_fixture(
            scored_fixture, articles, config
        )
        for environment_name, metric_name in (
            ("RADAR_REUSED_MANUAL_REVIEW_COUNT", "reused_manual_review_count"),
            ("RADAR_NEW_MANUAL_REVIEW_COUNT", "new_manual_review_count"),
        ):
            raw_count = os.environ.get(environment_name, "0")
            try:
                count = int(raw_count)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{environment_name} must be an integer") from error
            if count < 0 or str(count) != str(raw_count).strip():
                raise ValueError(f"{environment_name} must be non-negative")
            if count:
                controller.increment(metric_name, count)
                if metric_name == "new_manual_review_count":
                    controller.increment("manual_review_count", count)
        write_telemetry(
            os.path.join(reports_dir, "radar_llm_usage.json"),
            config,
            controller,
        )
        report_path = generate_markdown_report(
            scored_articles, config, deduplicate=True
        )
        write_news_selection_audit(reports_dir, ingestion, scored_articles, config)
        write_run_snapshot(
            reports_dir,
            ingestion,
            scored_articles,
            report_path,
            config,
        )
        print(f"\nReport generated successfully: {report_path}", flush=True)
        return report_path
    
    try:
        scoring = score_articles_pipeline(articles, config)
    except Exception as error:
        log_provider_error(
            logger,
            error,
            provider="configured_llm_chain",
            operation="scoring_pipeline",
            retryable=False,
            degraded_allowed=False,
        )
        # Both diagnostics are best effort; preserve the original scorer
        # failure and never turn an incomplete attempt into a run snapshot.
        for operation in (
            lambda: write_telemetry(
                os.path.join(reports_dir, "radar_llm_usage.json"),
                config,
                controller,
            ),
            lambda: write_scoring_failure_evidence(
                reports_dir, ingestion, articles, config, error
            ),
        ):
            try:
                operation()
            except Exception as audit_error:
                log_provider_error(
                    logger,
                    audit_error,
                    provider="local_artifact",
                    operation="scoring_failure_diagnostics",
                    retryable=False,
                    degraded_allowed=False,
                )
                print(
                    f"Failed to persist scoring failure diagnostics: {audit_error}",
                    flush=True,
                )
        raise
    scored_articles = list(scoring.articles)
    cache_data = scoring.cache_data
    cache_updates = scoring.cache_updates
    
    runtime_cost = config.get("_runtime", {}).get("llm_cost", {})
    new_reviewed = int(runtime_cost.get("ai_review_count", 0) or 0)
    deep_dive_enabled = os.environ.get("RADAR_ENABLE_DEEP_DIVE") == "1"
    if llm_calls_disabled(config):
        print(
            "LLM API mode disabled: skipping Deep Dive enrichment.",
            flush=True,
        )
    elif deep_dive_enabled and new_reviewed:
        scored_articles = enrich_deep_dives(
            scored_articles,
            cache_data,
            config,
            cache_updates=cache_updates,
        )
    else:
        print(
            "Skipping implicit Deep Dive: enable RADAR_ENABLE_DEEP_DIVE=1 "
            "and provide newly AI-reviewed articles to run it.",
            flush=True,
        )

    write_telemetry(
        os.path.join(reports_dir, "radar_llm_usage.json"),
        config,
        controller,
    )

    if policy.mode == "interactive":
        # The replay phase intentionally rewrites radar_llm_usage.json in
        # offline mode.  Preserve the prepare counters so the final telemetry
        # can distinguish verified historical reuse from genuinely new manual
        # review instead of reporting both as zero.
        write_telemetry(
            os.path.join(reports_dir, "radar_llm_usage_prepare.json"),
            config,
            controller,
        )
        request_path = config.get("_runtime", {}).get("llm_review_bundle_path")
        print(
            "Interactive review package prepared; production report generation "
            f"is paused until the audited response is imported: {request_path}",
            flush=True,
        )
        return request_path

    report_path = generate_markdown_report(
        scored_articles,
        config,
        # Keep all scored/evidence inputs, but collapse high-confidence
        # cross-publisher repeats in the report without an LLM call.
        deduplicate=True,
    )
    write_news_selection_audit(reports_dir, ingestion, scored_articles, config)
    write_run_snapshot(
        reports_dir,
        ingestion,
        scored_articles,
        report_path,
        config,
    )
    print(f"\nReport generated successfully: {report_path}", flush=True)
    
    # 5. Send Email
    # Email is now sent by the unified daily runner
    # send_email(report_path, config)

if __name__ == "__main__":
    main()
