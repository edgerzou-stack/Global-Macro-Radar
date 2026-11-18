"""Validated fixture loader for pre-scored manual/offline Industry Radar bundles."""

from __future__ import annotations

import json
import os

from llm_cost_policy import validate_unscored_priority_display
from score import (
    _apply_composite_scores,
    _validate_score_result,
    _validate_weights,
    validate_priority_industry_review,
)

SCORED_ARTICLES_FIXTURE_SCHEMA_VERSION = 1

_VALID_ENTRY_KEYS = (
    frozenset({"link", "score_data"}),
    frozenset({"link", "review_outcome"}),
    frozenset({"link", "review_outcome", "priority_display"}),
    frozenset({"link", "review_outcome", "priority_display", "priority_industry_review"}),
)


def _index_rss_articles_by_url(rss_articles):
    rss_by_url = {}
    for article in rss_articles:
        link = article.get("link")
        if not isinstance(link, str) or not link.strip() or link in rss_by_url:
            raise ValueError("RSS articles must have unique non-empty links")
        rss_by_url[link] = article
    return rss_by_url


def _index_fixture_entries_by_url(scores):
    score_by_url = {}
    for index, entry in enumerate(scores):
        if not isinstance(entry, dict) or frozenset(entry) not in _VALID_ENTRY_KEYS:
            raise ValueError(
                f"scored-articles fixture entry {index} must contain link and score_data"
            )
        link = entry.get("link")
        if not isinstance(link, str) or not link.strip() or link in score_by_url:
            raise ValueError(
                f"scored-articles fixture entry {index} has invalid/duplicate link"
            )
        if "review_outcome" in entry and entry["review_outcome"] != "unscored_priority":
            raise ValueError(
                "scored-articles fixture review_outcome must be unscored_priority"
            )
        score_by_url[link] = entry
    return score_by_url


def load_scored_articles_fixture(path, rss_articles, config):
    """Load, validate, and apply weights to a frozen scored-articles fixture."""
    fixture_path = os.path.abspath(os.fspath(path))
    try:
        with open(fixture_path, "r", encoding="utf-8") as handle:
            fixture = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Cannot load scored-articles fixture {fixture_path}: {error}"
        ) from error
    if not isinstance(fixture, dict) or set(fixture) != {"schema_version", "scores"}:
        raise ValueError("scored-articles fixture has invalid top-level fields")
    if fixture.get("schema_version") != SCORED_ARTICLES_FIXTURE_SCHEMA_VERSION:
        raise ValueError("unsupported scored-articles fixture schema_version")
    scores = fixture.get("scores")
    if not isinstance(scores, list):
        raise ValueError("scored-articles fixture scores must be a list")

    rss_by_url = _index_rss_articles_by_url(rss_articles)
    score_by_url = _index_fixture_entries_by_url(scores)
    if set(rss_by_url) != set(score_by_url):
        raise ValueError(
            "scored-articles fixture URL mismatch: "
            f"missing={sorted(set(rss_by_url) - set(score_by_url))}, "
            f"unexpected={sorted(set(score_by_url) - set(rss_by_url))}"
        )

    weights = _validate_weights(config)
    result = []
    for article_id, article in enumerate(rss_articles):
        raw_entry = score_by_url[article["link"]]
        if raw_entry.get("review_outcome") == "unscored_priority":
            scored = dict(article)
            scored["id"] = article_id
            scored["review_outcome"] = "unscored_priority"
            scored["priority_industry_review"] = validate_priority_industry_review(
                raw_entry.get("priority_industry_review"), article
            )
            if "priority_display" in raw_entry:
                scored["priority_display"] = validate_unscored_priority_display(
                    raw_entry["priority_display"]
                )
            result.append(scored)
            continue
        raw_score = raw_entry.get("score_data")
        if not isinstance(raw_score, dict):
            raise ValueError(
                f"scored-articles fixture score_data for {article['link']} must be an object"
            )
        scored = dict(article)
        scored["id"] = article_id
        scored["score_data"] = _apply_composite_scores(
            _validate_score_result(dict(raw_score)),
            weights,
        )
        result.append(scored)
    return result
