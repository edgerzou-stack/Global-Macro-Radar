import concurrent.futures
import difflib
import logging
import os
from dataclasses import dataclass

from cache_manager import (
    build_cache_key,
    get_cached_score,
    load_cache,
    make_cache_entry,
    save_cache,
)
from pipeline_selection import is_verified_deep_dive
from provider_errors import log_provider_error


logger = logging.getLogger(__name__)
SCORED_ARTICLES_FIXTURE_SCHEMA_VERSION = 1


def llm_calls_disabled():
    return os.environ.get("PIPELINE_DISABLE_LLM") == "1"


@dataclass(frozen=True)
class ScoringResult:
    articles: tuple
    cache_data: dict
    cache_updates: int


def scoring_cache_config(config):
    return {
        "industries": config.get("industries", []),
        "importance_criteria": config.get("importance_criteria", ""),
        "scoring_weights": config.get("scoring_weights", {}),
        "trusted_sources": config.get("trusted_sources", []),
        "language": config.get("output", {}).get("language", "Chinese"),
    }


def configured_scoring_identities(config, *, require_credentials=True):
    provider_keys = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }
    defaults = {
        "gemini": "gemini-2.5-flash",
        "openai": "gpt-4.1-mini",
        "deepseek": config.get("output", {}).get(
            "model",
            "deepseek-v4-flash",
        ),
    }
    providers = config.get("llm", {}).get("providers", {})
    identities = []
    for provider in config.get("llm", {}).get(
        "order",
        ["gemini", "openai", "deepseek"],
    ):
        settings = providers.get(provider, {})
        enabled = settings.get("enabled", True)
        if enabled and (
            not require_credentials or os.getenv(provider_keys.get(provider, ""))
        ):
            identities.append(
                (
                    provider,
                    settings.get(
                        "model",
                        defaults.get(provider, "unknown"),
                    ),
                )
            )
    return identities


def validate_scoring_configuration(config):
    identities = configured_scoring_identities(
        config,
        require_credentials=not llm_calls_disabled(),
    )
    if not identities:
        if llm_calls_disabled():
            raise ValueError(
                "CRITICAL ERROR: No enabled LLM provider identity is configured "
                "for read-only score-cache lookup while LLM calls are disabled."
            )
        raise ValueError(
            "CRITICAL ERROR: No enabled LLM provider has a configured API key. "
            "Check llm.order, llm.providers.*.enabled, and the corresponding "
            "GEMINI_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY."
        )
    return identities


def find_cached_article(cache_data, article, config):
    from score import SCORING_PROMPT_VERSION

    for provider, model in configured_scoring_identities(
        config,
        require_credentials=not llm_calls_disabled(),
    ):
        cache_key = build_cache_key(
            article,
            scoring_cache_config(config),
            SCORING_PROMPT_VERSION,
            provider,
            model,
        )
        score_data = get_cached_score(
            cache_data.get(cache_key),
            cache_key,
        )
        if score_data is not None:
            return score_data, cache_key
    return None, None


def store_article_score(
    cache_data,
    article,
    score_data,
    config,
    **extra,
):
    from score import SCORING_PROMPT_VERSION

    identities = configured_scoring_identities(config)
    provider = (
        score_data.get("llm_provider")
        if isinstance(score_data, dict)
        else None
    )
    model = (
        score_data.get("llm_model")
        if isinstance(score_data, dict)
        else None
    )
    if not provider or not model:
        if not identities:
            raise RuntimeError(
                "Cannot cache a score without a configured LLM identity"
            )
        provider, model = identities[0]
    cache_key = build_cache_key(
        article,
        scoring_cache_config(config),
        SCORING_PROMPT_VERSION,
        provider,
        model,
    )
    cache_data[cache_key] = make_cache_entry(
        cache_key,
        score_data,
        raw_title=article.get("title", ""),
        raw_summary=article.get("summary", ""),
        provider=provider,
        model=model,
        **extra,
    )
    article["_cache_key"] = cache_key
    return cache_key


def run_validated_batch(batch, config, scorer, attempts=2):
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            payload = scorer(batch, config)
            results = (
                payload.get("results")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(results, list) or len(results) != len(batch):
                count = len(results) if isinstance(results, list) else "invalid"
                raise ValueError(
                    f"batch result count {count} does not match input count "
                    f"{len(batch)}"
                )
            return results
        except Exception as error:
            last_error = error
            log_provider_error(
                logger,
                error,
                provider="configured_llm_chain",
                operation="validated_batch",
                retryable=attempt < attempts,
                degraded_allowed=False,
            )
            print(
                f"Validated batch attempt {attempt}/{attempts} failed: "
                f"{error}",
                flush=True,
            )
    raise RuntimeError(
        f"Validated batch failed after {attempts} attempts"
    ) from last_error


def load_scored_articles_fixture(path, rss_articles, config):
    import json

    from score import (
        _apply_composite_scores,
        _validate_score_result,
        _validate_weights,
    )

    fixture_path = os.path.abspath(os.fspath(path))
    try:
        with open(fixture_path, "r", encoding="utf-8") as handle:
            fixture = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Cannot load scored-articles fixture {fixture_path}: {error}"
        ) from error
    if (
        not isinstance(fixture, dict)
        or set(fixture) != {"schema_version", "scores"}
    ):
        raise ValueError(
            "scored-articles fixture has invalid top-level fields"
        )
    if (
        fixture.get("schema_version")
        != SCORED_ARTICLES_FIXTURE_SCHEMA_VERSION
    ):
        raise ValueError(
            "unsupported scored-articles fixture schema_version"
        )
    scores = fixture.get("scores")
    if not isinstance(scores, list):
        raise ValueError(
            "scored-articles fixture scores must be a list"
        )
    rss_by_url = {}
    for article in rss_articles:
        link = article.get("link")
        if (
            not isinstance(link, str)
            or not link.strip()
            or link in rss_by_url
        ):
            raise ValueError(
                "RSS articles must have unique non-empty links"
            )
        rss_by_url[link] = article
    score_by_url = {}
    for index, entry in enumerate(scores):
        if (
            not isinstance(entry, dict)
            or set(entry) != {"link", "score_data"}
        ):
            raise ValueError(
                f"scored-articles fixture entry {index} must contain "
                "link and score_data"
            )
        link = entry.get("link")
        if (
            not isinstance(link, str)
            or not link.strip()
            or link in score_by_url
        ):
            raise ValueError(
                f"scored-articles fixture entry {index} has "
                "invalid/duplicate link"
            )
        score_by_url[link] = entry.get("score_data")
    if set(rss_by_url) != set(score_by_url):
        raise ValueError(
            "scored-articles fixture URL mismatch: "
            f"missing={sorted(set(rss_by_url) - set(score_by_url))}, "
            f"unexpected={sorted(set(score_by_url) - set(rss_by_url))}"
        )
    weights = _validate_weights(config)
    result = []
    for article_id, article in enumerate(rss_articles):
        raw_score = score_by_url[article["link"]]
        if not isinstance(raw_score, dict):
            raise ValueError(
                "scored-articles fixture score_data for "
                f"{article['link']} must be an object"
            )
        score_data = _apply_composite_scores(
            _validate_score_result(dict(raw_score)),
            weights,
        )
        scored = dict(article)
        scored["id"] = article_id
        scored["score_data"] = score_data
        result.append(scored)
    return result


def _local_deduplicate(articles):
    def evidence_priority(article):
        """Choose evidence, never fetch order, as a duplicate representative."""
        tier_rank = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
        trade_eligible = article.get("trade_eligible")
        return (
            tier_rank.get(str(article.get("source_tier") or ""), 9),
            0 if article.get("source_lane") == "evidence" else 1,
            0 if trade_eligible is True else 1 if trade_eligible == "conditional" else 2,
            str(article.get("link") or ""),
        )

    groups = []
    for article in articles:
        text = article.get(
            "content",
            article.get("summary", ""),
        )[:800]
        candidate = (
            article.get("title", "") + " " + text
        ).lower()
        for group in groups:
            representative = group[0]
            representative_text = representative.get(
                "content",
                representative.get("summary", ""),
            )[:800]
            reference = (
                representative.get("title", "")
                + " "
                + representative_text
            ).lower()
            if difflib.SequenceMatcher(
                None,
                candidate,
                reference,
            ).ratio() > 0.85:
                group.append(article)
                break
        else:
            groups.append([article])
    # Similar secondary coverage must not erase the official record before the
    # scoring/evidence pipeline gets a chance to evaluate it.  The ordering is
    # deterministic so concurrent RSS completion order cannot change the
    # selected representative.
    return [min(group, key=evidence_priority) for group in groups]


def _rejected_score(
    article,
    *,
    event_type,
    prompt_version,
    reason,
    vague=False,
):
    return {
        "is_relevant": False,
        "is_vague_or_roundup": vague,
        "event_type": event_type,
        "industrial_claims": [],
        "market_only_claims": (
            [article.get("title", "")]
            if event_type == "market_only"
            else []
        ),
        "innovation_score": 0,
        "traffic_score": 0,
        "justification": reason,
        "translated_title": article["title"],
        "translated_summary": "",
        "prompt_version": prompt_version,
    }


def score_articles_pipeline(articles, config):
    from score import (
        SCORING_PROMPT_VERSION,
        apply_industry_relevance_gate,
        local_article_route,
        pre_filter_articles_batch,
        score_articles_batch,
    )

    validate_scoring_configuration(config)
    cache_data = load_cache()
    scored_articles = []
    new_articles = []
    updates = 0
    print(
        f"Loaded {len(cache_data)} articles from incremental cache.",
        flush=True,
    )
    disabled = llm_calls_disabled()
    print(
        "Loading cached Dual-Track scores with all LLM calls disabled..."
        if disabled
        else "Scoring articles using Dual-Track LLM...",
        flush=True,
    )
    for index, article in enumerate(articles):
        article["id"] = index
        score, cache_key = find_cached_article(
            cache_data,
            article,
            config,
        )
        if score is None:
            new_articles.append(article)
            continue
        score = apply_industry_relevance_gate(article, score)
        try:
            innovation = float(score.get("innovation_score", 0))
            traffic = float(score.get("traffic_score", 0))
        except (TypeError, ValueError):
            innovation = traffic = 0
        print(
            f"[{index + 1}/{len(articles)}] (Cached) "
            f"[I:{innovation:.1f} T:{traffic:.1f}] "
            f"{article['title'][:30]}...",
            flush=True,
        )
        article["score_data"] = score
        article["_cache_key"] = cache_key
        cached_deep_dive = cache_data[cache_key].get("deep_dive")
        if is_verified_deep_dive(cached_deep_dive):
            article["deep_dive"] = cached_deep_dive
        elif "deep_dive" in cache_data[cache_key]:
            cache_data[cache_key].pop("deep_dive", None)
            updates += 1
        scored_articles.append(article)
    print(
        f"Found {len(new_articles)} new articles to process.",
        flush=True,
    )
    if disabled:
        for article in new_articles:
            score = _rejected_score(
                article,
                event_type="unscored",
                prompt_version=SCORING_PROMPT_VERSION,
                reason="Unscored because PIPELINE_DISABLE_LLM=1",
                vague=True,
            )
            article["score_data"] = score
            scored_articles.append(article)
        runtime = config.setdefault("_runtime", {})
        runtime["llm_disabled"] = True
        runtime["llm_disabled_unscored_count"] = len(new_articles)
        print(
            "LLM calls are disabled; retained cached scores and marked "
            f"{len(new_articles)} uncached articles as unscored.",
            flush=True,
        )
        return ScoringResult(
            articles=tuple(scored_articles),
            cache_data=cache_data,
            cache_updates=updates,
        )
    original_new_count = len(new_articles)
    if new_articles:
        print("--- Phase 0: Local String Deduplication ---", flush=True)
    new_articles = _local_deduplicate(new_articles)
    if original_new_count:
        print(
            f"Reduced from {original_new_count} to "
            f"{len(new_articles)} unique events.",
            flush=True,
        )
    print("--- Phase 1: Pre-filtering (Batches of 20) ---", flush=True)
    passed = []
    pre_filter_input = []
    for article in new_articles:
        route = local_article_route(article)
        if route == "industry_candidate":
            passed.append(article)
            continue
        if route not in {"market_only", "reject"}:
            pre_filter_input.append(article)
            continue
        score = _rejected_score(
            article,
            event_type=(
                "market_only" if route == "market_only" else "non_industrial"
            ),
            prompt_version=SCORING_PROMPT_VERSION,
            reason="Filtered out by deterministic industry-news policy",
            vague=route == "reject",
        )
        article["score_data"] = score
        scored_articles.append(article)
        store_article_score(cache_data, article, score, config)
        updates += 1

    batches = [
        pre_filter_input[index : index + 20]
        for index in range(0, len(pre_filter_input), 20)
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(
                run_validated_batch,
                batch,
                config,
                pre_filter_articles_batch,
            )
            for batch in batches
        ]
        for future in concurrent.futures.as_completed(futures):
            for item in future.result():
                matched = next(
                    (
                        article
                        for article in new_articles
                        if article["id"] == item.get("id")
                    ),
                    None,
                )
                if matched is None:
                    continue
                if item.get("is_relevant", False):
                    passed.append(matched)
                    continue
                score = _rejected_score(
                    matched,
                    event_type="non_industrial",
                    prompt_version=SCORING_PROMPT_VERSION,
                    reason="Filtered out in Phase 1 (Pre-filter)",
                    vague=False,
                )
                matched["score_data"] = score
                scored_articles.append(matched)
                store_article_score(
                    cache_data,
                    matched,
                    score,
                    config,
                )
                updates += 1

    print(
        f"Phase 1 complete. {len(passed)} articles survived.",
        flush=True,
    )
    scoring_batches = [
        passed[index : index + 5]
        for index in range(0, len(passed), 5)
    ]
    if passed:
        print(
            "--- Phase 2: Detailed Scoring (Batches of 5) ---",
            flush=True,
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(
                run_validated_batch,
                batch,
                config,
                score_articles_batch,
            )
            for batch in scoring_batches
        ]
        for future in concurrent.futures.as_completed(futures):
            for item in future.result():
                matched = next(
                    (
                        article
                        for article in passed
                        if article["id"] == item.get("id")
                    ),
                    None,
                )
                if matched is None:
                    continue
                matched["score_data"] = apply_industry_relevance_gate(
                    matched,
                    {
                        key: value
                        for key, value in item.items()
                        if key != "id"
                    },
                )
                scored_articles.append(matched)
                try:
                    innovation = float(
                        matched["score_data"]["innovation_score"]
                    )
                    traffic = float(
                        matched["score_data"]["traffic_score"]
                    )
                except (TypeError, ValueError):
                    innovation = traffic = 0
                print(
                    f"  -> Scored [{matched['id']}] "
                    f"[I:{innovation:.1f} T:{traffic:.1f}] "
                    f"{matched['title'][:30]}",
                    flush=True,
                )
                store_article_score(
                    cache_data,
                    matched,
                    matched["score_data"],
                    config,
                )
                updates += 1
    if updates:
        save_cache(cache_data)
    return ScoringResult(
        articles=tuple(scored_articles),
        cache_data=cache_data,
        cache_updates=updates,
    )
