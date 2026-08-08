"""Validated source metadata for provenance-aware industry intelligence."""

import re
from pathlib import Path

import yaml

from event_contract import INDUSTRIAL_EVENT_TYPES


SOURCE_TIERS = {"T0", "T1", "T2", "T3"}
SOURCE_LANES = {"evidence", "discovery", "research"}
SOURCE_ADAPTERS = {"rss"}
TRADE_ELIGIBILITY = {True, False, "conditional"}
AUTHORITY_TYPES = INDUSTRIAL_EVENT_TYPES
EXPECTED_CADENCES = {"daily", "weekly", "monthly", "irregular"}


class SourceRegistryError(ValueError):
    pass


def _string_list(value, field, source_id, *, allow_empty=False):
    if not isinstance(value, list) or (not allow_empty and not value):
        raise SourceRegistryError(
            f"source {source_id!r} {field} must be a list"
            + ("" if allow_empty else " with at least one item")
        )
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise SourceRegistryError(
            f"source {source_id!r} {field} must contain non-empty strings"
        )
    return [item.strip() for item in value]


def load_source_registry(config):
    """Return registry entries keyed by URL and enforce RSS/metadata alignment."""
    raw_entries = config.get("source_registry")
    registry_file = config.get("source_registry_file")
    if raw_entries is not None and registry_file is not None:
        raise SourceRegistryError(
            "configure source_registry or source_registry_file, not both"
        )
    if registry_file is not None:
        if not isinstance(registry_file, str) or not registry_file.strip():
            raise SourceRegistryError("source_registry_file must be non-empty")
        path = Path(registry_file)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise SourceRegistryError(
                f"cannot load source_registry_file {path}: {error}"
            ) from error
        raw_entries = (
            payload.get("source_registry") if isinstance(payload, dict) else payload
        )
    feeds = config.get("rss_feeds", [])
    if raw_entries is None:
        if feeds:
            raise SourceRegistryError(
                "source registry alignment failed: rss_feeds are configured "
                "without source_registry metadata"
            )
        return {}
    if not isinstance(raw_entries, list):
        raise SourceRegistryError("source_registry must be a list")
    registry = {}
    seen_ids = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise SourceRegistryError(f"source_registry[{index}] must be an object")
        source_id = raw.get("id")
        if not isinstance(source_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]*", source_id
        ):
            raise SourceRegistryError(f"source_registry[{index}] has invalid id")
        if source_id in seen_ids:
            raise SourceRegistryError(f"duplicate source id {source_id!r}")
        seen_ids.add(source_id)
        url = raw.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise SourceRegistryError(f"source {source_id!r} has invalid url")
        if url in registry:
            raise SourceRegistryError(f"duplicate source url {url!r}")
        adapter = raw.get("adapter")
        tier = raw.get("tier")
        lane = raw.get("lane")
        if adapter not in SOURCE_ADAPTERS:
            raise SourceRegistryError(f"source {source_id!r} has invalid adapter")
        if tier not in SOURCE_TIERS:
            raise SourceRegistryError(f"source {source_id!r} has invalid tier")
        if lane not in SOURCE_LANES:
            raise SourceRegistryError(f"source {source_id!r} has invalid lane")
        trade_eligible = raw.get("trade_eligible")
        if trade_eligible not in TRADE_ELIGIBILITY:
            raise SourceRegistryError(
                f"source {source_id!r} has invalid trade_eligible"
            )
        if tier in {"T2", "T3"} and trade_eligible is not False:
            raise SourceRegistryError(
                f"source {source_id!r} secondary/social evidence cannot be trade eligible"
            )
        requires_corroboration = raw.get("requires_corroboration")
        if type(requires_corroboration) is not bool:
            raise SourceRegistryError(
                f"source {source_id!r} requires_corroboration must be boolean"
            )
        cadence = raw.get("expected_cadence")
        if cadence not in EXPECTED_CADENCES:
            raise SourceRegistryError(
                f"source {source_id!r} has invalid expected_cadence"
            )
        entry = dict(raw)
        entry["domains"] = _string_list(raw.get("domains"), "domains", source_id)
        entry["authority_for"] = _string_list(
            raw.get("authority_for", []),
            "authority_for",
            source_id,
            allow_empty=True,
        )
        unknown_authority = set(entry["authority_for"]) - AUTHORITY_TYPES
        if unknown_authority:
            raise SourceRegistryError(
                f"source {source_id!r} has unsupported authority_for values: "
                + ", ".join(sorted(unknown_authority))
            )
        if tier == "T0" and not entry["authority_for"]:
            raise SourceRegistryError(
                f"source {source_id!r} T0 authority_for must not be empty"
            )
        registry[url] = entry

    feed_urls = list(feeds)
    registry_rss_urls = [
        url for url, entry in registry.items() if entry["adapter"] == "rss"
    ]
    if len(feed_urls) != len(set(feed_urls)):
        raise SourceRegistryError("source registry alignment failed: duplicate rss_feeds")
    if set(feed_urls) != set(registry_rss_urls):
        missing = sorted(set(feed_urls) - set(registry_rss_urls))
        extra = sorted(set(registry_rss_urls) - set(feed_urls))
        raise SourceRegistryError(
            "source registry alignment failed: "
            f"unregistered={missing}, registry_only={extra}"
        )
    return registry


def _metadata(entry):
    return {
        "source_id": entry["id"],
        "source_tier": entry["tier"],
        "source_lane": entry["lane"],
        "source_domains": list(entry["domains"]),
        "authority_for": list(entry["authority_for"]),
        "trade_eligible": entry["trade_eligible"],
        "requires_corroboration": entry["requires_corroboration"],
        "expected_cadence": entry["expected_cadence"],
    }


def enrich_articles(articles, registry):
    enriched = []
    for article in articles:
        copied = dict(article)
        url = copied.get("feed_url")
        if url in registry:
            copied.update(_metadata(registry[url]))
        elif registry:
            raise SourceRegistryError(
                f"article {copied.get('title', '<untitled>')!r} lacks a registered feed_url"
            )
        enriched.append(copied)
    return enriched


def enrich_health(health, registry):
    enriched = []
    for item in health:
        copied = dict(item)
        url = copied.get("url")
        if url not in registry:
            if registry:
                raise SourceRegistryError(f"health entry has unregistered url {url!r}")
        else:
            copied.update(_metadata(registry[url]))
        enriched.append(copied)
    return enriched
