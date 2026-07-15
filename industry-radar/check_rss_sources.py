"""Standalone RSS health command using the same contract as production ingest."""

import logging
import os
import sys

import yaml

from ingest import fetch_rss_feeds
from main import save_json_atomic, validate_rss_health


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def main():
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if not os.path.exists(config_path):
        logging.error("Config file not found: %s", config_path)
        return 1
    with open(config_path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    feeds = config.get("rss_feeds", [])
    hours_back = config.get("output", {}).get("hours_back", 48)
    _, health = fetch_rss_feeds(
        feeds, hours_back=hours_back, return_health=True
    )
    artifact = os.path.join(os.path.dirname(__file__), "reports", "rss_health.json")
    save_json_atomic(artifact, health)
    try:
        validate_rss_health(
            health,
            float(config.get("output", {}).get("rss_max_failure_ratio", 0.5)),
        )
    except RuntimeError as error:
        logging.error("%s", error)
        return 1
    logging.info("RSS health passed; artifact written to %s", artifact)
    return 0


if __name__ == "__main__":
    sys.exit(main())
