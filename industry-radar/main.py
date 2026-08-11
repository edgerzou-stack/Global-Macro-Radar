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
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))
    config = load_config()
    reports_dir = os.environ.get("RADAR_REPORTS_DIR", "reports")
    
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
    if scored_fixture:
        print(
            f"Loading deterministic scored-articles fixture: {scored_fixture}",
            flush=True,
        )
        scored_articles = load_scored_articles_fixture(
            scored_fixture, articles, config
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
    
    if llm_calls_disabled():
        print(
            "PIPELINE_DISABLE_LLM=1: skipping LLM deep-dive enrichment.",
            flush=True,
        )
    else:
        scored_articles = enrich_deep_dives(
            scored_articles,
            cache_data,
            config,
            cache_updates=cache_updates,
        )

    report_path = generate_markdown_report(
        scored_articles,
        config,
        deduplicate=not llm_calls_disabled(),
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
