import concurrent.futures
import difflib
import hashlib
import json
import logging
import os
from pathlib import Path
from dataclasses import dataclass

from cache_manager import (
    build_cache_key,
    build_priority_display_cache_key,
    build_semantic_cache_key,
    get_cached_priority_display,
    get_cached_score,
    load_cache,
    load_priority_display_cache,
    make_cache_entry,
    make_priority_display_cache_entry,
    save_cache,
    save_priority_display_cache,
)
from llm_cost_policy import (
    LLMBudgetExceeded,
    _canonical_json,
    _write_json_atomic,
    active_run,
    record_runtime,
    resolve_policy,
    validate_unscored_priority_display,
    write_interactive_base_scores,
    write_manual_review_bundle,
)
from pipeline_selection import is_verified_deep_dive
from llm_router import LLMInsufficientBalance
from provider_errors import log_provider_error


logger = logging.getLogger(__name__)
SCORED_ARTICLES_FIXTURE_SCHEMA_VERSION = 1
UNSCORED_PLACEHOLDER_CONTRACT = "unscored-placeholder-v1"
UNSCORED_RESOLUTION = "unscored"
INTERACTIVE_MANUAL_RESOLUTION = "manual"
UNSCORED_REASON_CODES = frozenset(
    {
        "api_disabled_or_budget",
        "batch_budget_exhausted",
        "interactive_manual_pending",
    }
)


def llm_calls_disabled(config=None):
    if os.environ.get("PIPELINE_DISABLE_LLM") == "1":
        return True
    if config is None:
        return os.environ.get("RADAR_LLM_MODE", "offline") in {
            "offline",
            "interactive",
        }
    return not resolve_policy(config).api_enabled


@dataclass(frozen=True)
class ScoringResult:
    articles: tuple
    cache_data: dict
    cache_updates: int


def _attach_unscored_priority_display(article, display, *, cache_hit):
    """Attach only validated Chinese copy; never create a numerical score."""

    article["review_outcome"] = "unscored_priority"
    article["priority_display"] = validate_unscored_priority_display(display)
    article["_score_cache_hit"] = bool(cache_hit)
    article["_score_resolution"] = (
        "unscored_priority_display_cache" if cache_hit else "unscored_priority"
    )
    article["_priority_reason"] = "platform_restricted_subject_matter"


def scoring_rules_sha256():
    from event_contract import EVENT_TYPES
    from evidence_policy import EVIDENCE_POLICY_VERSION
    from score import (
        SCORING_RULE_VERSION,
        _INDUSTRIAL_ACTION_PATTERNS,
        _LOCAL_REJECTION_PATTERNS,
        _MARKET_ONLY_PATTERNS,
    )

    payload = {
        "scoring_rule_version": SCORING_RULE_VERSION,
        "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        "event_types": sorted(EVENT_TYPES),
        "market_patterns": [item.pattern for item in _MARKET_ONLY_PATTERNS],
        "industrial_patterns": [item.pattern for item in _INDUSTRIAL_ACTION_PATTERNS],
        "rejection_patterns": [item.pattern for item in _LOCAL_REJECTION_PATTERNS],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_BACKWARD_COMPATIBLE_SCORING_RULE_SHA256 = frozenset(
    {
        # ``deterministic-industry-boundary-v2`` before the weekly-roundup
        # rejection was added.  That addition is strictly more restrictive:
        # every reused score is passed through ``apply_industry_relevance_gate``
        # below, so a newly rejected roundup is zeroed before it can reach
        # selection or delivery.  Keeping this one audited predecessor avoids
        # turning an otherwise identical 100-item RSS capture into a new
        # manual-review batch merely because a local rejection rule tightened.
        "929df94457e42c1ba625bac41dd654ac99381f9ebca3a8937dac90a465e6f293",
    }
)


def compatible_scoring_rules_sha256():
    """Return approved cache-rule identities, newest first.

    This is deliberately an allowlist rather than a version-prefix rule.  A
    future scoring-policy change must opt in only after proving it is
    monotonic (that is, it cannot make an old accepted score less strict).
    """

    current = scoring_rules_sha256()
    return (current,) + tuple(
        rule_hash
        for rule_hash in sorted(_BACKWARD_COMPATIBLE_SCORING_RULE_SHA256)
        if rule_hash != current
    )


def scoring_prompt_identity():
    from score import SCORING_PROMPT_SHA256, SCORING_PROMPT_VERSION

    return f"{SCORING_PROMPT_VERSION}:{SCORING_PROMPT_SHA256}"


def semantic_cache_key(article, config):
    from score import SCORING_PROMPT_VERSION

    return build_semantic_cache_key(
        article,
        scoring_cache_config(config),
        scoring_prompt_identity(),
        scoring_rules_sha256(),
    )


def compatible_semantic_cache_keys(article, config):
    """Yield the exact cache identities that are safe to reuse for ``article``."""

    prompt_identity = scoring_prompt_identity()
    cache_config = scoring_cache_config(config)
    return tuple(
        build_semantic_cache_key(
            article,
            cache_config,
            prompt_identity,
            rule_hash,
        )
        for rule_hash in compatible_scoring_rules_sha256()
    )


def scoring_cache_config(config):
    from score import SCORING_RUBRIC_VERSION, scoring_rubric_sha256

    return {
        "industries": config.get("industries", []),
        "importance_criteria": config.get("importance_criteria", ""),
        "scoring_weights": config.get("scoring_weights", {}),
        "trusted_sources": config.get("trusted_sources", []),
        "language": config.get("output", {}).get("language", "Chinese"),
        "rubric_version": SCORING_RUBRIC_VERSION,
        "rubric_sha256": scoring_rubric_sha256(config),
    }


def configured_scoring_identities(config, *, require_credentials=True):
    policy = resolve_policy(config)
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
    order = config.get("llm", {}).get(
        "order",
        ["gemini", "openai", "deepseek"],
    )
    if policy.mode == "deepseek":
        order = ["deepseek"]
    for provider in order:
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
    policy = resolve_policy(config)
    if not policy.api_enabled:
        return []
    identities = configured_scoring_identities(
        config,
        require_credentials=True,
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

    for semantic_key, rule_hash in zip(
        compatible_semantic_cache_keys(article, config),
        compatible_scoring_rules_sha256(),
    ):
        semantic_entry = cache_data.get(semantic_key)
        score_data = get_cached_score(semantic_entry, semantic_key)
        if (
            score_data is not None
            and isinstance(semantic_entry, dict)
            and "manual_review_provenance" in semantic_entry
            and _verified_manual_review_provenance(
                semantic_entry,
                semantic_key,
                score_data,
                expected_rules_sha256=rule_hash,
            )
            is None
        ):
            continue
        if score_data is not None:
            return score_data, semantic_key
    # Backward-compatible, exact-provider lookup avoids a costly cold start for
    # valid v3 entries. A hit is migrated to the semantic key by the caller.
    for provider, model in configured_scoring_identities(
        config,
        require_credentials=False,
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


def _verified_manual_review_provenance(
    entry,
    semantic_key,
    score_data,
    *,
    expected_rules_sha256=None,
):
    """Return audited reuse provenance or ``None`` for an ordinary cache hit."""

    if not isinstance(entry, dict):
        return None
    provenance = entry.get("manual_review_provenance")
    if not isinstance(provenance, dict) or provenance.get("schema_version") != 1:
        return None
    required_text = {
        "source_run_id",
        "source_effective_date",
        "request_sha256",
        "request_file_sha256",
        "request_path",
        "response_file_sha256",
        "response_path",
        "receipt_file_sha256",
        "receipt_path",
        "output_sha256",
        "output_path",
        "reviewer",
        "prompt_version",
        "rules_sha256",
        "config_sha256",
        "response_contract_sha256",
        "semantic_input_sha256",
        "score_data_sha256",
    }
    if any(
        not isinstance(provenance.get(field), str) or not provenance.get(field)
        for field in required_text
    ):
        return None
    if provenance.get("semantic_input_sha256") != semantic_key:
        return None
    if provenance.get("prompt_version") != scoring_prompt_identity():
        return None
    expected_rules_sha256 = (
        scoring_rules_sha256()
        if expected_rules_sha256 is None
        else expected_rules_sha256
    )
    if provenance.get("rules_sha256") != expected_rules_sha256:
        return None
    score_sha256 = hashlib.sha256(
        _canonical_json(score_data).encode("utf-8")
    ).hexdigest()
    if provenance.get("score_data_sha256") != score_sha256:
        return None
    for field in (
        "request_sha256",
        "request_file_sha256",
        "response_file_sha256",
        "receipt_file_sha256",
        "output_sha256",
        "config_sha256",
        "response_contract_sha256",
        "score_data_sha256",
    ):
        value = provenance[field]
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            return None
    artifact_fields = (
        ("request_path", "request_file_sha256"),
        ("response_path", "response_file_sha256"),
        ("receipt_path", "receipt_file_sha256"),
        ("output_path", "output_sha256"),
    )
    artifact_paths = []
    for path_field, hash_field in artifact_fields:
        artifact_path = Path(provenance[path_field]).expanduser().resolve()
        if not artifact_path.is_file():
            return None
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if digest != provenance[hash_field]:
            return None
        artifact_paths.append(artifact_path)
    if len({path.parent for path in artifact_paths}) != 1:
        return None
    radar_dir = artifact_paths[0].parent
    run_dir = radar_dir.parent
    if run_dir.name != provenance.get("source_run_id"):
        return None
    production_state = run_dir / "gemini-production-state.json"
    invocation_state = run_dir / "folder-agent-operator-invocation.json"
    try:
        if production_state.is_file():
            state = json.loads(production_state.read_text(encoding="utf-8"))
            # Only a fully healthy production run may publish a reusable
            # manual-review result.  Degraded attempts are terminal failures.
            allowed_status = {"completed"}
            expected_component = "folder-agent-production-state"
        elif invocation_state.is_file():
            state = json.loads(invocation_state.read_text(encoding="utf-8"))
            allowed_status = {"completed"}
            expected_component = "folder-agent-full-flow-invocation"
        else:
            return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(state, dict)
        or state.get("component") != expected_component
        or state.get("run_id") != provenance.get("source_run_id")
        or state.get("reviewer") != provenance.get("reviewer")
        or state.get("status") not in allowed_status
    ):
        return None
    return dict(provenance)


def store_article_score(
    cache_data,
    article,
    score_data,
    config,
    **extra,
):
    from score import SCORING_PROMPT_VERSION

    identities = configured_scoring_identities(config, require_credentials=False)
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
        provider = str(extra.pop("provenance_provider", "deterministic"))
        model = str(extra.pop("provenance_model", scoring_rules_sha256()))
        if provider != "deterministic" and identities:
            provider, model = identities[0]
    cache_key = semantic_cache_key(article, config)
    cache_data[cache_key] = make_cache_entry(
        cache_key,
        score_data,
        raw_title=article.get("title", ""),
        raw_summary=article.get("summary", ""),
        provider=provider,
        model=model,
        semantic_cache_key=cache_key,
        rules_sha256=scoring_rules_sha256(),
        prompt_version=scoring_prompt_identity(),
        **extra,
    )
    article["_cache_key"] = cache_key
    return cache_key


def run_validated_batch(batch, config, scorer, attempts=2, *, preauthorize=False):
    if attempts < 1:
        raise ValueError("attempts must be at least one")
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            if preauthorize:
                controller = active_run(config)
                controller.authorize_api_call(
                    "configured_llm_chain",
                    f"{getattr(scorer, '__name__', 'validated_batch')}:{len(batch)}",
                )
                controller.mark_next_router_call_preauthorized()
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
            expected_ids = [item.get("id") for item in batch]
            returned_ids = [
                item.get("id") if isinstance(item, dict) else None
                for item in results
            ]
            if (
                len(set(expected_ids)) != len(expected_ids)
                or any(not isinstance(item, dict) for item in results)
                or len(set(returned_ids)) != len(returned_ids)
                or set(returned_ids) != set(expected_ids)
            ):
                raise ValueError("batch result ids do not exactly match input ids")
            return results
        except (LLMBudgetExceeded, LLMInsufficientBalance):
            raise
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
        validate_priority_industry_review,
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
        if not isinstance(entry, dict) or set(entry) not in (
            {"link", "score_data"},
            {"link", "review_outcome"},
            {"link", "review_outcome", "priority_display"},
            {"link", "review_outcome", "priority_display", "priority_industry_review"},
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
        if (
            "review_outcome" in entry
            and entry["review_outcome"] != "unscored_priority"
        ):
            raise ValueError(
                "scored-articles fixture review_outcome must be "
                "unscored_priority"
            )
        score_by_url[link] = entry
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
        text = (
            article.get("content") or article.get("summary") or ""
        )[:800]
        candidate = (
            article.get("title", "") + " " + text
        ).lower()
        for group in groups:
            representative = group[0]
            representative_text = (
                representative.get("content")
                or representative.get("summary")
                or ""
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


def deterministic_deduplicate_articles(articles, _config=None):
    """Deduplicate without an LLM or summary synthesis side effect."""

    return _local_deduplicate(list(articles))


def _has_review_material(article):
    """Return whether the frozen feed captured evidence beyond the title."""

    return bool(
        str(article.get("content") or "").strip()
        or str(article.get("summary") or "").strip()
    )


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
        "barrier_to_entry": "none",
        "market_size": "none",
        "immediacy": "none",
        "reasoning_chain": reason,
        "tech_score": 0,
        "commercial_score": 0,
        "hype_score": 0,
        "macro_score": 0,
        "justification": reason,
        "translated_title": article["title"],
        "translated_summary": "",
        "prompt_version": prompt_version,
    }


def _assign_unscored_placeholder(
    article,
    *,
    config,
    prompt_version,
    reason,
    reason_code,
    resolution=UNSCORED_RESOLUTION,
):
    """Attach one audited placeholder contract across every deferred branch."""
    if reason_code not in UNSCORED_REASON_CODES:
        raise ValueError(f"unsupported unscored reason code: {reason_code}")
    if resolution not in {UNSCORED_RESOLUTION, INTERACTIVE_MANUAL_RESOLUTION}:
        raise ValueError(f"unsupported score resolution: {resolution}")
    if (resolution == INTERACTIVE_MANUAL_RESOLUTION) != (
        reason_code == "interactive_manual_pending"
    ):
        raise ValueError("interactive manual resolution/reason mismatch")
    score = _rejected_score(
        article,
        event_type="unscored",
        prompt_version=prompt_version,
        reason=reason,
        vague=True,
    )
    score["_unscored_placeholder_contract"] = UNSCORED_PLACEHOLDER_CONTRACT
    score["_unscored_reason_code"] = reason_code
    article["score_data"] = score
    article["_score_cache_hit"] = False
    article["_score_resolution"] = resolution
    article["_cache_key"] = semantic_cache_key(article, config)
    return score


def score_articles_pipeline(articles, config):
    from score import (
        SCORING_PROMPT_VERSION,
        apply_industry_relevance_gate,
        local_article_route,
        pre_filter_articles_batch,
        requires_unscored_priority,
        score_articles_batch,
        translate_unscored_priority_batch,
        unscored_priority_industry_eligibility,
    )

    policy = resolve_policy(config)
    capture_input_sha256 = hashlib.sha256(
        _canonical_json(
            [
                {
                    key: article.get(key)
                    for key in (
                        "title", "summary", "content", "link", "published_at",
                        "source", "source_id", "source_tier", "source_lane",
                        "authority_for",
                    )
                }
                for article in articles
            ]
        ).encode("utf-8")
    ).hexdigest()
    # ``main`` owns the run lifecycle.  Reuse its controller so the telemetry
    # artifact and the budget ledger describe the same API calls.  Standalone
    # callers still get a controller lazily through ``active_run(config)``.
    controller = active_run(config)
    validate_scoring_configuration(config)
    cache_data = load_cache()
    scored_articles = []
    new_articles = []
    priority_articles = []
    priority_display_cache = None
    priority_display_cache_hits = 0
    priority_display_cache_misses = 0
    priority_filtered_reasons = {}
    updates = 0
    print(
        f"Loaded {len(cache_data)} articles from incremental cache.",
        flush=True,
    )
    disabled = not policy.api_enabled
    print(
        "Loading cached Dual-Track scores with all LLM calls disabled..."
        if disabled
        else "Scoring articles using Dual-Track LLM...",
        flush=True,
    )
    for index, article in enumerate(articles):
        article["id"] = index
        # Restricted material never reaches the numerical score cache or
        # scorer.  It must still pass the deterministic industry boundary
        # before any Chinese display is created or reused.
        if policy.api_enabled and requires_unscored_priority(article):
            eligible, reason = unscored_priority_industry_eligibility(article)
            controller.increment("deterministic_count")
            if not eligible:
                article["_score_resolution"] = "unscored_priority_filtered"
                article["_priority_reason"] = reason
                priority_filtered_reasons[reason] = (
                    priority_filtered_reasons.get(reason, 0) + 1
                )
                continue
            if priority_display_cache is None:
                priority_display_cache = load_priority_display_cache()
            priority_key = build_priority_display_cache_key(article)
            cached_display = get_cached_priority_display(
                priority_display_cache.get(priority_key), priority_key
            )
            if cached_display is not None:
                try:
                    _attach_unscored_priority_display(
                        article, cached_display, cache_hit=True
                    )
                except ValueError:
                    # Cache data is untrusted. A malformed entry must not reach
                    # the report and is treated as a fresh translation miss.
                    priority_articles.append(article)
                    priority_display_cache_misses += 1
                    controller.increment("priority_display_cache_miss_count")
                else:
                    priority_display_cache_hits += 1
                    controller.increment("priority_display_cache_hit_count")
                    scored_articles.append(article)
            else:
                priority_articles.append(article)
                priority_display_cache_misses += 1
                controller.increment("priority_display_cache_miss_count")
            controller.increment("deterministic_count")
            continue
        score, cache_key = find_cached_article(
            cache_data,
            article,
            config,
        )
        if score is None:
            controller.increment("cache_miss_count")
            new_articles.append(article)
            continue
        controller.increment("cache_hit_count")
        score = apply_industry_relevance_gate(article, score)
        try:
            tech = int(score.get("tech_score", 0) or 0)
            comm = int(score.get("commercial_score", 0) or 0)
        except (TypeError, ValueError):
            tech = comm = 0
        print(
            f"[{index + 1}/{len(articles)}] (Cached) "
            f"[Tech:{tech} Comm:{comm}] "
            f"{article['title'][:30]}...",
            flush=True,
        )
        article["score_data"] = score
        article["_score_cache_hit"] = True
        article["_score_resolution"] = "cache"
        semantic_key = semantic_cache_key(article, config)
        article["_cache_key"] = semantic_key
        compatible_key_rules = dict(
            zip(
                compatible_semantic_cache_keys(article, config),
                compatible_scoring_rules_sha256(),
            )
        )
        cache_entry = cache_data.get(cache_key)
        provenance = _verified_manual_review_provenance(
            cache_entry,
            cache_key,
            (
                cache_entry.get("score_data")
                if isinstance(cache_entry, dict)
                else score
            ),
            expected_rules_sha256=compatible_key_rules.get(
                cache_key,
                scoring_rules_sha256(),
            ),
        )
        if provenance is not None:
            article["_manual_review_reuse"] = provenance
            controller.increment("reused_manual_review_count")
        if cache_key != semantic_key and not isinstance(
            cache_entry.get("manual_review_provenance")
            if isinstance(cache_entry, dict)
            else None,
            dict,
        ):
            cache_data[semantic_key] = make_cache_entry(
                semantic_key,
                score,
                raw_title=article.get("title", ""),
                raw_summary=article.get("summary", ""),
                provider=cache_entry.get("provider", "legacy"),
                model=cache_entry.get("model", "legacy"),
                semantic_cache_key=semantic_key,
                rules_sha256=scoring_rules_sha256(),
                prompt_version=scoring_prompt_identity(),
            )
            updates += 1
        cached_deep_dive = cache_entry.get("deep_dive")
        if is_verified_deep_dive(cached_deep_dive):
            article["deep_dive"] = cached_deep_dive
        elif "deep_dive" in cache_entry:
            cache_entry.pop("deep_dive", None)
            updates += 1
        scored_articles.append(article)
    if priority_articles:
        print(
            "--- Priority Display: translation-only restricted articles ---",
            flush=True,
        )
        priority_batch_size = min(20, policy.max_articles_per_run)
        for batch_start in range(0, len(priority_articles), priority_batch_size):
            batch = priority_articles[
                batch_start : batch_start + priority_batch_size
            ]
            # A translation failure is intentionally fatal: sending an English
            # fallback would violate the Chinese priority-display contract.
            translated = run_validated_batch(
                batch,
                config,
                translate_unscored_priority_batch,
                preauthorize=True,
            )
            display_by_id = {
                item["id"]: item["priority_display"]
                for item in translated
            }
            for article in batch:
                display = display_by_id[article["id"]]
                _attach_unscored_priority_display(
                    article, display, cache_hit=False
                )
                priority_key = build_priority_display_cache_key(article)
                priority_display_cache[priority_key] = (
                    make_priority_display_cache_entry(
                        priority_key, article, article["priority_display"]
                    )
                )
                scored_articles.append(article)
            # Preserve each completed batch before the rest of the full flow.
            # A later operational failure must not make the same news consume
            # another translation request on a same-day rerun.
            save_priority_display_cache(priority_display_cache)
    runtime = config.setdefault("_runtime", {})
    runtime["unscored_priority_count"] = (
        len(priority_articles) + priority_display_cache_hits
    )
    runtime["unscored_priority_filtered_count"] = sum(
        priority_filtered_reasons.values()
    )
    runtime["unscored_priority_filtered_reasons"] = dict(
        sorted(priority_filtered_reasons.items())
    )
    runtime["priority_display_cache_hit_count"] = priority_display_cache_hits
    runtime["priority_display_cache_miss_count"] = priority_display_cache_misses
    print(
        f"Found {len(new_articles)} new articles to process.",
        flush=True,
    )
    original_new_count = len(new_articles)
    if new_articles:
        print("--- Phase 0: Local String Deduplication ---", flush=True)
    # A folder-AI request is an auditable compilation unit. It must cover every
    # cache miss in the sealed RSS input; an API-oriented article budget or a
    # second lossy deduplication pass must never make entries disappear.
    if policy.mode != "interactive" and not policy.complete_review:
        new_articles = _local_deduplicate(new_articles)
    duplicate_count = original_new_count - len(new_articles)
    if duplicate_count:
        controller.increment("deterministic_count", duplicate_count)
    if original_new_count:
        print(
            f"Reduced from {original_new_count} to "
            f"{len(new_articles)} unique events.",
            flush=True,
        )
    review_candidates = []
    for article in new_articles:
        if not _has_review_material(article):
            score = _rejected_score(
                article,
                event_type="non_industrial",
                prompt_version=SCORING_PROMPT_VERSION,
                reason=(
                    "Excluded from manual review because the frozen feed "
                    "contains no body or summary evidence"
                ),
                vague=True,
            )
            article["score_data"] = score
            article["_score_cache_hit"] = False
            article["_score_resolution"] = "deterministic"
            scored_articles.append(article)
            store_article_score(
                cache_data,
                article,
                score,
                config,
                provenance_provider="deterministic",
                provenance_model=scoring_rules_sha256(),
            )
            controller.increment("deterministic_count")
            config.setdefault("_runtime", {}).setdefault(
                "insufficient_review_material_count", 0
            )
            config["_runtime"]["insufficient_review_material_count"] += 1
            updates += 1
            continue
        route = local_article_route(article)
        if route not in {"market_only", "reject"}:
            review_candidates.append(article)
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
        article["_score_cache_hit"] = False
        article["_score_resolution"] = "deterministic"
        scored_articles.append(article)
        store_article_score(
            cache_data,
            article,
            score,
            config,
            provenance_provider="deterministic",
            provenance_model=scoring_rules_sha256(),
        )
        controller.increment("deterministic_count")
        updates += 1

    admitted = []
    deferred = []
    if policy.api_enabled and controller.has_initial_api_capacity():
        article_by_key = {
            semantic_cache_key(article, config): article
            for article in review_candidates
        }
        admitted_keys, blocked_keys = controller.admit_articles(article_by_key)
        admitted = [article_by_key[key] for key in article_by_key if key in admitted_keys]
        deferred = [article_by_key[key] for key in article_by_key if key in blocked_keys]
    elif policy.api_enabled:
        deferred = list(review_candidates)
        controller.increment("budget_blocked_count", len(deferred))
    elif policy.mode == "interactive":
        deferred = list(review_candidates)
    else:
        deferred = list(review_candidates)

    for article in deferred:
        interactive_pending = policy.mode == "interactive"
        _assign_unscored_placeholder(
            article,
            config=config,
            prompt_version=SCORING_PROMPT_VERSION,
            reason=(
                "Awaiting interactive folder-AI review"
                if interactive_pending
                else "Unscored because API calls are disabled or budget exhausted"
            ),
            reason_code=(
                "interactive_manual_pending"
                if interactive_pending
                else "api_disabled_or_budget"
            ),
            resolution=(
                INTERACTIVE_MANUAL_RESOLUTION
                if interactive_pending
                else UNSCORED_RESOLUTION
            ),
        )
        scored_articles.append(article)

    runtime = config.setdefault("_runtime", {})
    runtime["llm_disabled"] = disabled
    runtime["llm_disabled_unscored_count"] = len(deferred) if disabled else 0
    scoring_batches = [
        admitted[index : index + 5]
        for index in range(0, len(admitted), 5)
    ]
    if admitted:
        print(
            "--- AI Review: Detailed Scoring (Batches of 5) ---",
            flush=True,
        )
    for batch_index, batch in enumerate(scoring_batches, start=1):
        try:
            results = run_validated_batch(
                batch,
                config,
                score_articles_batch,
                preauthorize=True,
            )
        except LLMBudgetExceeded:
            controller.increment("ai_review_count", -len(batch))
            controller.increment("budget_blocked_count", max(0, len(batch) - 1))
            for article in batch:
                _assign_unscored_placeholder(
                    article,
                    config=config,
                    prompt_version=SCORING_PROMPT_VERSION,
                    reason="Unscored because LLM budget was exhausted",
                    reason_code="batch_budget_exhausted",
                )
                scored_articles.append(article)
            continue
        for item in results:
                matched = next(
                    (
                        article
                        for article in admitted
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
                matched["_score_cache_hit"] = False
                matched["_score_resolution"] = "ai"
                scored_articles.append(matched)
                try:
                    tech = int(
                        matched["score_data"].get("tech_score", 0) or 0
                    )
                    comm = int(
                        matched["score_data"].get("commercial_score", 0) or 0
                    )
                except (TypeError, ValueError):
                    tech = comm = 0
                print(
                    f"  -> Scored [{matched['id']}] "
                    f"[Tech:{tech} Comm:{comm}] "
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
        # A completed provider batch is run-local audit evidence.  It is not
        # a reusable global score cache while this run may still fail.
        snapshot = controller.snapshot()
        batch_payload = {
            "schema_version": 1,
            "component": "validated-scoring-batch",
            "run_id": controller.run_id,
            "effective_date": controller.effective_date,
            "code_source_sha256": os.environ.get("PIPELINE_CODE_SOURCE_SHA256", ""),
            "batch_index": batch_index,
            "capture_article_count": len(articles),
            "capture_input_sha256": capture_input_sha256,
            "policy_sha256": snapshot["policy_sha256"],
            "scoring_config_sha256": hashlib.sha256(
                _canonical_json(scoring_cache_config(config)).encode("utf-8")
            ).hexdigest(),
            "prompt_identity": scoring_prompt_identity(),
            "rules_sha256": scoring_rules_sha256(),
            "validated_score_count": len(batch),
            "results": [
                {
                    "id": article["id"],
                    "link": article.get("link"),
                    "source": article.get("source"),
                    "source_tier": article.get("source_tier"),
                    "semantic_input_sha256": semantic_cache_key(article, config),
                    "score_data": article["score_data"],
                }
                for article in batch
            ],
        }
        batch_payload["batch_sha256"] = hashlib.sha256(
            _canonical_json(batch_payload).encode("utf-8")
        ).hexdigest()
        _write_json_atomic(
            Path(os.environ.get("RADAR_REPORTS_DIR", "reports"))
            / "validated-scoring-batches"
            / f"batch-{batch_index:04d}.json",
            batch_payload,
        )
        config.setdefault("_runtime", {}).setdefault(
            "validated_scoring_batch_paths", []
        ).append(
            str(
                (
                    Path(os.environ.get("RADAR_REPORTS_DIR", "reports"))
                    / "validated-scoring-batches"
                    / f"batch-{batch_index:04d}.json"
                ).resolve()
            )
        )
    if policy.mode == "interactive":
        runtime = config.setdefault("_runtime", {})
        reuse_items = []
        for article in scored_articles:
            provenance = article.get("_manual_review_reuse")
            if not isinstance(provenance, dict):
                continue
            # The source review's semantic identity must remain immutable for
            # audit and revocation.  A monotonic local-policy update can give
            # the same RSS item a new *current* base-score identity, though.
            # Bind both explicitly instead of silently replacing either one.
            reuse_items.append(
                {
                    "link": article.get("link"),
                    "resolved_score_data_sha256": hashlib.sha256(
                        _canonical_json(article.get("score_data")).encode("utf-8")
                    ).hexdigest(),
                    **provenance,
                    "resolved_base_semantic_input_sha256": semantic_cache_key(
                        article,
                        config,
                    ),
                }
            )
        reuse_items.sort(
            key=lambda item: item["resolved_base_semantic_input_sha256"]
        )
        reuse_manifest = {
            "schema_version": 2,
            "component": "manual-review-reuse",
            "run_id": controller.run_id,
            "effective_date": controller.effective_date,
            "reused_item_count": len(reuse_items),
            "items": reuse_items,
        }
        reuse_manifest["manifest_sha256"] = hashlib.sha256(
            _canonical_json(reuse_manifest).encode("utf-8")
        ).hexdigest()
        reuse_path = Path(
            os.environ.get("RADAR_REPORTS_DIR", "reports")
        ) / "llm-review-reuse.json"
        _write_json_atomic(reuse_path, reuse_manifest)
        runtime["manual_review_reuse_path"] = str(reuse_path.resolve())
        rss_fixture_path = runtime.get("interactive_rss_fixture_path")
        if not rss_fixture_path:
            # Keep the low-level API usable for isolated callers. The complete
            # resumable contract is intentionally enabled only when main has
            # sealed ingestion health together with the article input.
            request_path = Path(
                os.environ.get("RADAR_LLM_REVIEW_REQUEST")
                or Path(os.environ.get("RADAR_REPORTS_DIR", "reports"))
                / "llm-review-request.json"
            )
            write_manual_review_bundle(
                request_path,
                [
                    article for article in scored_articles
                    if article.get("_score_resolution") == "manual"
                ],
                config=config,
                prompt_version=scoring_prompt_identity(),
                rules_sha256=scoring_rules_sha256(),
                semantic_key_for=lambda item: semantic_cache_key(item, config),
                controller=controller,
            )
        else:
            reports_dir = Path(os.environ.get("RADAR_REPORTS_DIR", "reports"))
            base_path = Path(
                os.environ.get("RADAR_LLM_BASE_SCORES")
                or reports_dir / "llm-review-base-scores.json"
            )
            request_path = Path(
                os.environ.get("RADAR_LLM_REVIEW_REQUEST")
                or reports_dir / "llm-review-request.json"
            )
            # Reconstruct in the sealed RSS order instead of trusting transient
            # in-memory ids. This also proves the base is neither missing nor
            # adding a URL before a reviewer sees the request.
            with open(rss_fixture_path, "r", encoding="utf-8") as handle:
                rss_payload = json.load(handle)
            rss_links = [item.get("link") for item in rss_payload.get("articles", [])]
            scored_by_link = {
                item.get("link"): item for item in scored_articles
                if isinstance(item.get("link"), str)
            }
            if (
                len(scored_by_link) != len(scored_articles)
                or set(scored_by_link) != set(rss_links)
            ):
                raise RuntimeError(
                    "interactive scored URL set does not match sealed RSS input"
                )
            complete = [scored_by_link[link] for link in rss_links]
            write_interactive_base_scores(
                base_path,
                complete,
                config=config,
                prompt_version=scoring_prompt_identity(),
                rules_sha256=scoring_rules_sha256(),
                rss_fixture_path=rss_fixture_path,
                controller=controller,
            )
            manual_articles = [
                article for article in complete
                if article.get("_score_resolution") == "manual"
            ]
            write_manual_review_bundle(
                request_path,
                manual_articles,
                config=config,
                prompt_version=scoring_prompt_identity(),
                rules_sha256=scoring_rules_sha256(),
                semantic_key_for=lambda item: semantic_cache_key(item, config),
                controller=controller,
                rss_fixture_path=rss_fixture_path,
                base_scores_path=base_path,
            )
            runtime["interactive_base_scores_path"] = str(base_path.resolve())
            runtime["llm_review_bundle_path"] = str(request_path.resolve())
            if not manual_articles:
                from import_manual_review import compile_without_manual_review

                compiled_path = Path(
                    os.environ.get("RADAR_LLM_COMPILED_SCORES")
                    or reports_dir / "llm-review-scored-articles.json"
                )
                _, receipt_path = compile_without_manual_review(
                    request_path,
                    compiled_path,
                )
                runtime["interactive_compiled_scores_path"] = str(
                    compiled_path.resolve()
                )
                runtime["interactive_import_receipt_path"] = str(
                    receipt_path.resolve()
                )
                runtime["no_manual_review_needed"] = True
    elif updates:
        save_cache(cache_data)
    record_runtime(config, controller)
    return ScoringResult(
        articles=tuple(scored_articles),
        cache_data=cache_data,
        cache_updates=updates,
    )
