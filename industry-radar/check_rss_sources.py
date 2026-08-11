"""Standalone RSS health command using the same contract as production ingest."""

import logging
import os
import sys

import yaml

from ingest import fetch_rss_feeds
from main import save_json_atomic, validate_rss_health
from source_registry import enrich_health, load_source_registry


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def main():
    config_path = os.environ.get(
        "RADAR_CONFIG", os.path.join(os.path.dirname(__file__), "config.yaml")
    )
    if not os.path.exists(config_path):
        logging.error("Config file not found: %s", config_path)
        return 1
    with open(config_path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    feeds = config.get("rss_feeds", [])
    hours_back = config.get("output", {}).get("hours_back", 48)
    articles, health = fetch_rss_feeds(
        feeds, hours_back=hours_back, return_health=True
    )
    registry = (
        load_source_registry(config)
        if config.get("source_registry") is not None
        or config.get("source_registry_file") is not None
        else {}
    )
    health = enrich_health(health, registry)
    reports_dir = os.environ.get(
        "RADAR_REPORTS_DIR", os.path.join(os.path.dirname(__file__), "reports")
    )
    artifact = os.path.join(reports_dir, "rss_health.json")
    save_json_atomic(artifact, health)
    try:
        validate_rss_health(
            health,
            float(config.get("output", {}).get("rss_max_failure_ratio", 0.5)),
            min_available_ratio=float(
                config.get("output", {}).get(
                    "rss_min_available_ratio",
                    config.get("output", {}).get("rss_min_healthy_ratio", 0.0),
                )
            ),
            min_fresh_sources=int(
                config.get("output", {}).get("rss_min_fresh_sources", 1)
            ),
            min_total_fresh_entries=int(
                config.get("output", {}).get("rss_min_total_fresh_entries", 1)
            ),
            min_configured_sources=int(
                config.get("output", {}).get("rss_min_configured_sources", 1)
            ),
            article_count=len(articles),
            critical_source_groups=config.get("rss_critical_source_groups", []),
            min_primary_available_sources=int(
                config.get("output", {}).get(
                    "rss_min_primary_available_sources", 0
                )
            ),
            min_primary_fresh_entry_share=float(
                config.get("output", {}).get(
                    "rss_min_primary_fresh_entry_share_warning", 0.0
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
    except (RuntimeError, ValueError) as error:
        logging.error("%s", error)
        return 1
    logging.info("RSS health passed; artifact written to %s", artifact)
    return 0


if __name__ == "__main__":
    sys.exit(main())
