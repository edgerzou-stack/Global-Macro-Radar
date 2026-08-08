import json
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from ingest import fetch_rss_feeds, load_rss_fixture
from pipeline_health import (
    rss_reference_time_utc,
    validate_rss_fixture_effective_date,
    validate_rss_health,
)
from pipeline_selection import deduplicate_input_articles
from run_date import logical_today
from source_registry import enrich_articles, enrich_health, load_source_registry


@dataclass(frozen=True)
class IngestionResult:
    articles: tuple
    health: tuple
    reference_time: datetime
    duplicate_count: int
    health_summary: dict


def collect_articles(
    config,
    *,
    save_health,
    load_fixture=load_rss_fixture,
    fetch_feeds=fetch_rss_feeds,
):
    hours_back = config.get("output", {}).get("hours_back", 48)
    registry = (
        load_source_registry(config)
        if config.get("source_registry") is not None
        or config.get("source_registry_file") is not None
        else {}
    )
    fixture = os.environ.get("RADAR_RSS_FIXTURE")
    if fixture:
        print(
            f"Loading deterministic RSS fixture: {fixture}",
            flush=True,
        )
        articles, health = load_fixture(fixture)
        effective_date = logical_today()
        validate_rss_fixture_effective_date(
            articles,
            health,
            effective_date,
        )
        reference_time = datetime.combine(
            effective_date + timedelta(days=1),
            time.min,
            tzinfo=timezone.utc,
        ) - timedelta(microseconds=1)
    else:
        reference_time = rss_reference_time_utc()
        articles, health = fetch_feeds(
            config.get("rss_feeds", []),
            hours_back=hours_back,
            now=reference_time,
            return_health=True,
        )
    articles = enrich_articles(articles, registry)
    health = enrich_health(health, registry)
    save_health(health)
    health_summary = validate_rss_health(
        health,
        max_failure_ratio=float(
            config.get("output", {}).get(
                "rss_max_failure_ratio",
                0.5,
            )
        ),
        min_healthy_ratio=float(
            config.get("output", {}).get(
                "rss_min_healthy_ratio",
                0.0,
            )
        ),
        min_fresh_sources=int(
            config.get("output", {}).get(
                "rss_min_fresh_sources",
                1,
            )
        ),
        min_total_fresh_entries=int(
            config.get("output", {}).get(
                "rss_min_total_fresh_entries",
                1,
            )
        ),
        min_configured_sources=int(
            config.get("output", {}).get(
                "rss_min_configured_sources",
                1,
            )
        ),
        article_count=len(articles),
        critical_source_groups=config.get(
            "rss_critical_source_groups",
            [],
        ),
        reference_time=reference_time,
        max_fresh_entry_share=float(
            config.get("output", {}).get(
                "rss_max_source_fresh_entry_share",
                0.5,
            )
        ),
        min_primary_available_sources=int(
            config.get("output", {}).get(
                "rss_min_primary_available_sources",
                0,
            )
        ),
        min_primary_fresh_entry_share=float(
            config.get("output", {}).get(
                "rss_min_primary_fresh_entry_share_warning",
                0.0,
            )
        ),
        required_primary_domains=config.get(
            "rss_required_primary_domains", []
        ),
        min_primary_available_per_domain=int(
            config.get("output", {}).get(
                "rss_min_primary_available_per_domain", 0
            )
        ),
        min_primary_current_per_domain=int(
            config.get("output", {}).get(
                "rss_min_primary_current_per_domain", 0
            )
        ),
    )
    print(
        "RSS_HEALTH_SUMMARY "
        + json.dumps(health_summary, ensure_ascii=False, sort_keys=True),
        flush=True,
    )
    unique = deduplicate_input_articles(articles)
    return IngestionResult(
        articles=tuple(unique),
        health=tuple(health),
        reference_time=reference_time,
        duplicate_count=len(articles) - len(unique),
        health_summary=health_summary,
    )
