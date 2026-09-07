import yaml
import os
import json
import tempfile
import hashlib
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
from pipeline_selection import is_verified_deep_dive
from ingest import fetch_rss_feeds, load_rss_fixture
from run_date import logical_date_text


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
    return {
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
            scored_articles, config, deduplicate=False
        )
        write_run_snapshot(
            reports_dir,
            ingestion,
            scored_articles,
            report_path,
            config,
        )
        print(f"\nReport generated successfully: {report_path}", flush=True)
        return report_path
    
    scoring = score_articles_pipeline(articles, config)
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
        # Ingestion and scoring already perform deterministic content/URL
        # deduplication. Report rendering must never trigger an implicit LLM.
        deduplicate=False,
    )
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
