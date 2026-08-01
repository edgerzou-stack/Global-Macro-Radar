import concurrent.futures
import json
import logging

from cache_manager import (
    DEEP_DIVE_POLICY_VERSION,
    is_fresh_deep_dive_miss,
    make_deep_dive_miss,
    save_cache,
)
from pipeline_scoring import store_article_score
from pipeline_selection import is_verified_deep_dive
from provider_errors import log_provider_error


logger = logging.getLogger(__name__)


def emit_pipeline_metric(component, *, dimensions=None, **counters):
    print(
        "PIPELINE_METRIC "
        + json.dumps(
            {
                "component": component,
                "counters": counters,
                **({"dimensions": dimensions} if dimensions else {}),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


def run_deep_dive_job(
    article,
    cache_data,
    config,
    *,
    generator,
    now=None,
):
    cache_key = article.get("_cache_key")
    if is_verified_deep_dive(article.get("deep_dive")):
        emit_pipeline_metric(
            "deep_dive_cache",
            hit=1,
            saved_external_calls=1,
            dimensions={"policy_version": DEEP_DIVE_POLICY_VERSION},
        )
        return None, None, None, False
    article.pop("deep_dive", None)
    cache_entry = cache_data.get(cache_key, {}) if cache_key else {}
    cached_deep_dive = cache_entry.get("deep_dive")
    if is_verified_deep_dive(cached_deep_dive):
        article["deep_dive"] = cached_deep_dive
        emit_pipeline_metric(
            "deep_dive_cache",
            hit=1,
            saved_external_calls=1,
            dimensions={"policy_version": DEEP_DIVE_POLICY_VERSION},
        )
        return None, None, None, False
    if is_fresh_deep_dive_miss(cache_entry, now=now):
        emit_pipeline_metric(
            "deep_dive_cache",
            negative_hit=1,
            saved_external_calls=1,
            dimensions={"policy_version": DEEP_DIVE_POLICY_VERSION},
        )
        print(
            f"Skipping Deep Dive for {article['title'][:30]} "
            "(recent verified-primary miss cache).",
            flush=True,
        )
        return None, None, None, False
    emit_pipeline_metric(
        "deep_dive_cache",
        miss=1,
        dimensions={"policy_version": DEEP_DIVE_POLICY_VERSION},
    )
    print(
        f"Generating Deep Dive for {article['title'][:30]}...",
        flush=True,
    )
    return article, cache_key, generator(article, config), True


def enrich_deep_dives(
    scored_articles,
    cache_data,
    config,
    *,
    cache_updates=0,
):
    from deep_dive import generate_deep_dive_report
    from score import apply_industry_relevance_gate

    for article in scored_articles:
        article["score_data"] = apply_industry_relevance_gate(
            article,
            article.get("score_data", {}),
        )
    candidates = [
        article
        for article in scored_articles
        if article.get("score_data", {}).get("is_relevant")
        and article.get("score_data", {}).get("innovation_score", 0)
        + article.get("score_data", {}).get("traffic_score", 0)
        >= 18
    ]
    updated = False
    if candidates:
        print(
            f"Checking Deep Dive for {len(candidates)} highly rated "
            "articles (concurrently)...",
            flush=True,
        )
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=5
        ) as executor:
            futures = [
                executor.submit(
                    run_deep_dive_job,
                    article,
                    cache_data,
                    config,
                    generator=generate_deep_dive_report,
                )
                for article in candidates
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    article, cache_key, deep_dive, attempted = (
                        future.result()
                    )
                    if article is None:
                        continue
                    if not cache_key:
                        cache_key = store_article_score(
                            cache_data,
                            article,
                            article["score_data"],
                            config,
                        )
                    if is_verified_deep_dive(deep_dive):
                        article["deep_dive"] = deep_dive
                        cache_data[cache_key]["deep_dive"] = deep_dive
                        cache_data[cache_key].pop(
                            "deep_dive_miss",
                            None,
                        )
                        emit_pipeline_metric(
                            "deep_dive_cache",
                            write=1,
                            dimensions={
                                "policy_version": DEEP_DIVE_POLICY_VERSION
                            },
                        )
                        updated = True
                    elif attempted:
                        cache_data[cache_key].pop("deep_dive", None)
                        cache_data[cache_key]["deep_dive_miss"] = (
                            make_deep_dive_miss(
                                "no_verified_primary_or_source_unavailable"
                            )
                        )
                        emit_pipeline_metric(
                            "deep_dive_cache",
                            negative_write=1,
                            dimensions={
                                "policy_version": DEEP_DIVE_POLICY_VERSION
                            },
                        )
                        updated = True
                except Exception as error:
                    log_provider_error(
                        logger,
                        error,
                        provider="deep_dive_source_chain",
                        operation="generate_deep_dive",
                        retryable=False,
                        degraded_allowed=True,
                    )
                    print(
                        f"Error in deep dive worker: {error}",
                        flush=True,
                    )
    if cache_updates > 0 or updated:
        save_cache(cache_data)
    return scored_articles
