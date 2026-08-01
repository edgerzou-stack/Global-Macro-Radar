import yaml
import os
import json
import tempfile
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
    run_validated_batch,
    score_articles_pipeline,
    scoring_cache_config,
    store_article_score,
    validate_scoring_configuration,
)
from pipeline_selection import is_verified_deep_dive
from ingest import fetch_rss_feeds, load_rss_fixture


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
        print(f"\nReport generated successfully: {report_path}", flush=True)
        return report_path
    
    scoring = score_articles_pipeline(articles, config)
    scored_articles = list(scoring.articles)
    cache_data = scoring.cache_data
    cache_updates = scoring.cache_updates
    
    scored_articles = enrich_deep_dives(
        scored_articles,
        cache_data,
        config,
        cache_updates=cache_updates,
    )
        
    report_path = generate_markdown_report(scored_articles, config)
    print(f"\nReport generated successfully: {report_path}", flush=True)
    
    # 5. Send Email
    # Email is now sent by the unified daily runner
    # send_email(report_path, config)

if __name__ == "__main__":
    main()
