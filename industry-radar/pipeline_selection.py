from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from collections import Counter
import math
import re

from evidence_policy import (
    annotate_article_evidence,
    attach_same_batch_primary_corroboration,
    research_watch_decision,
)
from url_identity import canonicalize_article_url


REPORT_SCORE_HARD_FLOOR = 8.0
UNSCORED_PLACEHOLDER_CONTRACT = "unscored-placeholder-v1"


def review_is_pending(article):
    """Distinguish an unfinished review from a completed manual score."""
    resolution = article.get("_score_resolution")
    if resolution in {"unscored", "interactive_manual_pending"}:
        return True
    score = article.get("score_data") or {}
    return (
        resolution == "manual"
        and isinstance(score, dict)
        and score.get("_unscored_placeholder_contract")
        == UNSCORED_PLACEHOLDER_CONTRACT
    )


@dataclass(frozen=True)
class ReportSelection:
    supernova: tuple
    hardcore: tuple
    hype: tuple
    strategic_watch: tuple
    deep_dives: tuple
    evidence_input: tuple
    evidence_selection: tuple
    diagnostics: dict


def normalize_score(value):
    """Return the canonical two-decimal score used by every report decision."""
    try:
        score = Decimal(str(value)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, TypeError, ValueError):
        score = Decimal("0.00")
    return float(max(Decimal("0.00"), min(Decimal("10.00"), score)))


def report_score_threshold(config):
    """Return the effective presentation threshold, which cannot fall below 8."""
    configured = config.get("output", {}).get(
        "min_score_to_keep",
        REPORT_SCORE_HARD_FLOOR,
    )
    if isinstance(configured, bool):
        raise ValueError("min_score_to_keep must be numeric")
    try:
        configured = float(configured)
    except (TypeError, ValueError) as error:
        raise ValueError("min_score_to_keep must be numeric") from error
    if not math.isfinite(configured) or configured < 0:
        raise ValueError("min_score_to_keep must be a finite non-negative number")
    return max(REPORT_SCORE_HARD_FLOOR, configured)


def report_score(article):
    """Return the sole score used to admit an article to any report lane."""
    score = article.get("score_data") or {}
    return max(
        normalize_score(score.get("innovation_score", 0)),
        normalize_score(score.get("traffic_score", 0)),
    )


_EVENT_TITLE_WORD = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")
_EVENT_TITLE_STOPWORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "is", "it", "its", "new", "now", "of", "on", "or", "the", "to",
    "two", "with", "model", "models", "api", "apis", "lower", "cheaper",
    "cost", "costs", "price", "pricing", "claims", "claim", "more",
    "release", "releases", "released", "launch", "launches", "launched",
    "unveil", "unveils", "unveiled", "introduce", "introduces",
    "introducing", "debut", "debuts", "announces", "announced",
})
_EVENT_RELEASE_WORDS = frozenset({
    "release", "releases", "released", "launch", "launches", "launched",
    "unveil", "unveils", "unveiled", "introduce", "introduces",
    "introducing", "debut", "debuts", "announces", "announced",
})
_EVENT_DEPLOY_WORDS = frozenset({
    "available", "availability", "deploy", "deploys", "deployed",
    "integrates", "integrated", "adds", "rolls", "rollout",
})
_EVENT_FUNDING_WORDS = frozenset({"raises", "raised", "secures", "secured", "funding"})
_EVENT_UPDATE_WORDS = frozenset({
    "patch", "patches", "patched", "fix", "fixes", "fixed", "update",
    "updates", "updated", "upgrade", "upgrades", "upgraded",
})


def _report_event_title_features(article):
    """Extract conservative, source-visible anchors; never infer an event."""
    raw = _EVENT_TITLE_WORD.findall(str(article.get("title") or "").casefold())
    words = set(raw)
    if words & _EVENT_UPDATE_WORDS:
        action = "update"
    elif words & _EVENT_DEPLOY_WORDS:
        action = "deployment"
    elif words & _EVENT_FUNDING_WORDS:
        action = "funding"
    elif words & _EVENT_RELEASE_WORDS:
        action = "release"
    else:
        action = ""
    tokens = frozenset(
        word for word in raw
        if word not in _EVENT_TITLE_STOPWORDS and not word.isdecimal()
    )
    anchor = ""
    for index, word in enumerate(raw):
        if not any(char.isdigit() for char in word):
            continue
        if any(char.isalpha() for char in word):
            anchor = word
            break
        if "." in word and index > 0:
            anchor = f"{raw[index - 1]}:{word}"
            break
    return action, anchor, tokens


def _same_report_event(first, second):
    first_type = str((first.get("score_data") or {}).get("event_type") or "")
    second_type = str((second.get("score_data") or {}).get("event_type") or "")
    if not first_type or first_type != second_type:
        return False
    first_action, first_anchor, first_tokens = _report_event_title_features(first)
    second_action, second_anchor, second_tokens = _report_event_title_features(second)
    if not first_anchor or first_anchor != second_anchor:
        return False
    # Only release coverage has a reliable event anchor here. Follow-up
    # deployments, patches and funding can be distinct industrial events even
    # when they share the same model and version in their headlines.
    if first_action != "release" or second_action != "release":
        return False
    shared = first_tokens & second_tokens
    union = first_tokens | second_tokens
    return len(shared) >= 3 and len(shared) / len(union) >= 0.25


def deterministic_report_event_deduplicate(articles, config=None):
    """Collapse only high-confidence same-event report coverage, without API calls.

    Every original article remains in the scored/evidence input. The chosen
    representative favors stronger source evidence when its report score is
    close to the best score; no article text or numerical score is synthesized.
    """
    items = list(articles)
    for article in items:
        article.pop("_report_event_representative_link", None)
    ordered = sorted(items, key=lambda item: _stable_rank_key(item, report_score))
    groups = []
    for article in ordered:
        for group in groups:
            if all(_same_report_event(article, member) for member in group):
                group.append(article)
                break
        else:
            groups.append([article])

    tolerance = float(
        (config or {}).get("output", {}).get("primary_evidence_score_tolerance", 0.75)
    )
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("primary_evidence_score_tolerance must be non-negative")
    tier_rank = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
    retained = []
    for group in groups:
        best_score = max(report_score(item) for item in group)
        near_best = [
            item for item in group
            if report_score(item) >= best_score - tolerance
        ]
        representative = min(
            near_best,
            key=lambda item: (
                -int(item.get("trade_evidence_eligible") is True),
                -int(_is_primary_supported(item)),
                tier_rank.get(str(item.get("source_tier") or ""), 9),
                -report_score(item),
                canonicalize_article_url(item.get("link") or ""),
            ),
        )
        retained.append(representative)
        for item in group:
            if item is not representative:
                item["_report_event_representative_link"] = representative.get("link") or ""
    return sorted(retained, key=lambda item: _stable_rank_key(item, report_score))


def is_verified_deep_dive(deep_dive):
    return (
        isinstance(deep_dive, dict)
        and deep_dive.get("evidence_mode") == "verified_primary"
        and bool(deep_dive.get("primary_url"))
        and bool(deep_dive.get("report_content"))
    )


def deduplicate_input_articles(articles):
    unique = []
    seen_urls = set()
    seen_titles = set()
    for article in articles:
        link = canonicalize_article_url(article["link"])
        title = " ".join(article["title"].lower().split())
        if link not in seen_urls and title not in seen_titles:
            unique.append(article)
            seen_urls.add(link)
            seen_titles.add(title)
    return unique


_SUPPORTED_EVIDENCE_STATES = frozenset(
    {"authoritative_record", "primary_claim", "primary_supported"}
)


def _is_primary_supported(article):
    return str(article.get("evidence_state") or "") in _SUPPORTED_EVIDENCE_STATES


def _stable_rank_key(article, score_key, *, supported_first=False):
    """Return a total ordering so equal scores cannot inherit fetch timing."""
    try:
        score = float(score_key(article))
    except (TypeError, ValueError):
        score = 0.0
    return (
        -int(supported_first and _is_primary_supported(article)),
        -score,
        canonicalize_article_url(article.get("link") or ""),
        " ".join(str(article.get("title") or "").casefold().split()),
    )


def _evidence_preference_key(article):
    """Prefer the strongest auditable representative of one reported event."""
    tier_rank = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
    score = article.get("score_data") or {}
    return (
        -int(article.get("trade_evidence_eligible") is True),
        tier_rank.get(str(article.get("source_tier") or ""), 9),
        -max(
            normalize_score(score.get("innovation_score", 0)),
            normalize_score(score.get("traffic_score", 0)),
        ),
        canonicalize_article_url(article.get("link") or ""),
    )


def _deduplicate_evidence_articles(articles):
    """Deduplicate the evidence lane without letting commentary erase proof.

    Report presentation may prefer the most readable/highest-scoring article.
    The machine-readable trade boundary must instead prefer a trade-eligible
    authoritative record for the same normalized event title.
    """
    groups = {}
    order = []
    for article in articles:
        link = canonicalize_article_url(article.get("link") or "")
        title = " ".join(str(article.get("title") or "").casefold().split())
        event_type = str((article.get("score_data") or {}).get("event_type") or "")
        identity = f"title:{event_type}:{title}" if title else f"url:{link}"
        if identity not in groups:
            groups[identity] = []
            order.append(identity)
        groups[identity].append(article)
    return [
        min(groups[identity], key=_evidence_preference_key)
        for identity in order
    ]


def _bounded_evidence_aware(
    items,
    *,
    score_key,
    limit,
    target_ratio,
    tolerance,
    enforce_ratio=False,
):
    """Fill a report section without replacing materially stronger reporting.

    The score threshold has already been applied. A supported item may replace
    the weakest discovery-only item only when it is within ``tolerance`` score
    points. This improves provenance when equivalent evidence exists without
    manufacturing coverage from low-value filler.
    """
    ordered = sorted(
        items,
        key=lambda item: _stable_rank_key(item, score_key),
    )
    chosen = list(ordered[:limit])
    exclusion_reasons = {}
    if not chosen or target_ratio <= 0:
        return chosen, 0, exclusion_reasons
    required = math.ceil(target_ratio * len(chosen))
    supported = sum(_is_primary_supported(item) for item in chosen)
    candidates = [item for item in ordered[limit:] if _is_primary_supported(item)]
    while supported < required and candidates:
        replacement = candidates.pop(0)
        replaceable = [
            (index, item)
            for index, item in enumerate(chosen)
            if not _is_primary_supported(item)
            and score_key(replacement) >= score_key(item) - tolerance
        ]
        if not replaceable:
            break
        index, displaced = max(
            replaceable,
            key=lambda pair: _stable_rank_key(pair[1], score_key),
        )
        chosen[index] = replacement
        exclusion_reasons[id(displaced)] = (
            "replaced_by_primary_evidence_within_score_tolerance"
        )
        supported += 1
    excluded = 0
    if enforce_ratio:
        while chosen and (
            sum(_is_primary_supported(item) for item in chosen) / len(chosen)
            < target_ratio
        ):
            unsupported = [
                (index, item)
                for index, item in enumerate(chosen)
                if not _is_primary_supported(item)
            ]
            if not unsupported:
                break
            index, removed = max(
                unsupported,
                key=lambda pair: _stable_rank_key(pair[1], score_key),
            )
            chosen.pop(index)
            exclusion_reasons[id(removed)] = "excluded_by_primary_evidence_ratio"
            excluded += 1
    return (
        sorted(
            chosen,
            key=lambda item: _stable_rank_key(
                item,
                score_key,
                supported_first=True,
            ),
        ),
        excluded,
        exclusion_reasons,
    )


def select_report_articles(
    scored_articles,
    config,
    report_date,
    *,
    deduplicate=True,
    relevance_gate,
    deduplicator,
):
    minimum = report_score_threshold(config)
    strategic_config = config.get("strategic_hardtech", {})
    strategic_enabled = strategic_config.get("enabled") is True
    lookback_days = config.get("output", {}).get(
        "report_days_lookback",
        2,
    )
    date_text = report_date.isoformat()
    cutoff = (report_date - timedelta(days=lookback_days)).isoformat()
    selected = []
    evidence_candidates = []
    report_exclusion_reasons = {}
    evidence_input_rejections = Counter()
    report_score_threshold_excluded = 0
    relevant_articles = []
    corroboration_articles = []
    for article in scored_articles:
        published = article.get("published_at", "")[:10]
        if published and (published < cutoff or published > date_text):
            evidence_input_rejections["outside_effective_window"] += 1
            continue
        # A captured primary record can corroborate a separate reviewed story
        # even while its own display score is unfinished.
        corroboration_articles.append(article)
        if review_is_pending(article):
            evidence_input_rejections["review_pending"] += 1
            continue
        score = relevance_gate(article, article.get("score_data", {}))
        article["score_data"] = score
        if not score.get("is_relevant"):
            evidence_input_rejections[
                str(score.get("industry_policy_rejection") or "not_relevant")
            ] += 1
            continue
        score = dict(score)
        innovation = normalize_score(score.get("innovation_score", 0))
        traffic = normalize_score(score.get("traffic_score", 0))
        score["innovation_score"] = innovation
        score["traffic_score"] = traffic
        article["score_data"] = score
        relevant_articles.append(article)

    # Corroboration is resolved only inside the effective window. A low-scored
    # official record may still support research provenance, while the T1 trade
    # gate separately requires its T0 corroborator to pass relevance policy.
    attach_same_batch_primary_corroboration(corroboration_articles)
    for article in relevant_articles:
        score = article["score_data"]
        innovation = score["innovation_score"]
        traffic = score["traffic_score"]
        annotate_article_evidence(article)
        # The machine-readable evidence boundary evaluates every relevant,
        # in-window event. Numeric scores remain a report presentation rule and
        # can no longer prevent a valid official event from reaching policy.
        evidence_candidates.append(article)
        if innovation >= minimum or traffic >= minimum:
            selected.append(article)
        else:
            report_score_threshold_excluded += 1
            if strategic_enabled:
                watch_decision = research_watch_decision(article)
                article["research_watch_decision"] = watch_decision
    evidence_input = tuple(evidence_candidates)
    evidence_selection = tuple(_deduplicate_evidence_articles(evidence_candidates))
    evidence_trade_count = sum(
        article.get("trade_evidence_eligible") is True
        for article in evidence_selection
    )
    evidence_rejection_reasons = Counter(
        str(
            (article.get("trade_evidence_decision") or {}).get("reason")
            or "missing_trade_evidence_decision"
        )
        for article in evidence_selection
        if article.get("trade_evidence_eligible") is not True
    )
    if selected and deduplicate:
        before_deduplication = list(selected)
        selected = deduplicator(selected, config)
        retained_ids = {id(item) for item in selected}
        for item in before_deduplication:
            if id(item) not in retained_ids:
                report_exclusion_reasons[id(item)] = (
                    "duplicate_event_report"
                    if item.get("_report_event_representative_link")
                    else "deduplicated_from_report_selection"
                )

    max_discovery_per_source = config.get("output", {}).get(
        "max_selected_per_discovery_source"
    )
    if max_discovery_per_source is not None:
        if (
            type(max_discovery_per_source) is not int
            or max_discovery_per_source <= 0
        ):
            raise ValueError(
                "max_selected_per_discovery_source must be a positive integer"
            )
        bounded = []
        discovery_counts = Counter()
        for article in sorted(
            selected,
            key=lambda item: _stable_rank_key(item, report_score),
        ):
            if article.get("source_lane") == "discovery":
                source_id = str(
                    article.get("source_id") or article.get("source") or "unknown"
                )
                if discovery_counts[source_id] >= max_discovery_per_source:
                    report_exclusion_reasons[id(article)] = (
                        "excluded_by_discovery_source_cap"
                    )
                    continue
                discovery_counts[source_id] += 1
            bounded.append(article)
        selected = bounded

    output_config = config.get("output", {})
    max_items_per_section = output_config.get("max_items_per_section", 10)
    target_primary_ratio = output_config.get(
        "report_min_primary_supported_ratio", 0.0
    )
    evidence_score_tolerance = output_config.get(
        "primary_evidence_score_tolerance", 0.75
    )
    enforce_primary_ratio = output_config.get(
        "enforce_report_primary_supported_ratio", False
    )
    if type(max_items_per_section) is not int or max_items_per_section <= 0:
        raise ValueError("max_items_per_section must be a positive integer")
    try:
        target_primary_ratio = float(target_primary_ratio)
        evidence_score_tolerance = float(evidence_score_tolerance)
    except (TypeError, ValueError) as error:
        raise ValueError("primary evidence selection settings must be numeric") from error
    if not 0 <= target_primary_ratio <= 1:
        raise ValueError("report_min_primary_supported_ratio must be within [0, 1]")
    if evidence_score_tolerance < 0:
        raise ValueError("primary_evidence_score_tolerance must be non-negative")
    if type(enforce_primary_ratio) is not bool:
        raise ValueError(
            "enforce_report_primary_supported_ratio must be boolean"
        )

    supernova = []
    hardcore = []
    hype = []
    deep_dives = []
    for article in selected:
        score = article.get("score_data", {})
        innovation = score.get("innovation_score", 0)
        traffic = score.get("traffic_score", 0)
        if (
            innovation + traffic >= 18
            and is_verified_deep_dive(article.get("deep_dive"))
        ):
            deep_dives.append(article)
        if innovation >= minimum and traffic >= minimum:
            supernova.append(article)
        elif innovation >= minimum:
            hardcore.append(article)
        elif traffic >= minimum:
            hype.append(article)
    supernova, supernova_excluded, supernova_exclusion_reasons = _bounded_evidence_aware(
        supernova,
        score_key=lambda item: item["score_data"].get("innovation_score", 0)
        + item["score_data"].get("traffic_score", 0),
        limit=max_items_per_section,
        target_ratio=target_primary_ratio,
        tolerance=evidence_score_tolerance * 2,
        enforce_ratio=enforce_primary_ratio,
    )
    hardcore, hardcore_excluded, hardcore_exclusion_reasons = _bounded_evidence_aware(
        hardcore,
        score_key=lambda item: item["score_data"].get("innovation_score", 0),
        limit=max_items_per_section,
        target_ratio=target_primary_ratio,
        tolerance=evidence_score_tolerance,
        enforce_ratio=enforce_primary_ratio,
    )
    hype, hype_excluded, hype_exclusion_reasons = _bounded_evidence_aware(
        hype,
        score_key=lambda item: item["score_data"].get("traffic_score", 0),
        limit=max_items_per_section,
        target_ratio=target_primary_ratio,
        tolerance=evidence_score_tolerance,
        enforce_ratio=enforce_primary_ratio,
    )
    evidence_exclusion_reasons = {
        **supernova_exclusion_reasons,
        **hardcore_exclusion_reasons,
        **hype_exclusion_reasons,
    }
    ratio_research = sorted(
        (
            item
            for item in selected
            if evidence_exclusion_reasons.get(id(item))
            == "excluded_by_primary_evidence_ratio"
        ),
        key=lambda item: _stable_rank_key(
            item,
            lambda candidate: max(
                candidate["score_data"].get("innovation_score", 0),
                candidate["score_data"].get("traffic_score", 0),
            ),
        ),
    )
    # Research Watch remains a report lane, so it may only contain articles
    # that passed the same hard score gate as every main section.  Low-scored
    # official records remain in ``evidence_input`` and can still corroborate
    # or qualify evidence independently of report presentation.
    strategic_watch = list(ratio_research[:max_items_per_section])
    max_per_topic = strategic_config.get("max_items_per_topic", 2)
    if type(max_per_topic) is not int or max_per_topic <= 0:
        raise ValueError("max_items_per_topic must be a positive integer")
    rendered = supernova + hardcore + hype
    source_counts = Counter(
        str(item.get("source") or "unknown") for item in rendered
    )
    leading_source, leading_count = (
        source_counts.most_common(1)[0] if source_counts else ("", 0)
    )
    selected_count = len(rendered)
    evidence_counts = Counter(
        str(item.get("evidence_state") or "discovery_only") for item in rendered
    )
    primary_supported = sum(
        evidence_counts[state]
        for state in (
            "authoritative_record",
            "primary_claim",
            "primary_supported",
        )
    )
    near_hardcore = sum(
        minimum - 0.5
        <= item["score_data"].get("innovation_score", 0)
        < minimum
        for item in rendered
    )
    rendered_ids = {id(item) for item in rendered}
    research_watch_ids = {id(item) for item in strategic_watch}
    report_rendered_ids = rendered_ids | research_watch_ids
    report_items = rendered + strategic_watch
    if any(report_score(item) < minimum for item in report_items):
        raise AssertionError("report lane contains an article below the score threshold")
    selection_decisions = [
        {
            "title": str(item.get("title") or ""),
            "link": str(item.get("link") or ""),
            "source": str(item.get("source") or item.get("source_id") or "unknown"),
            "evidence_state": str(item.get("evidence_state") or "discovery_only"),
            "innovation_score": item.get("score_data", {}).get("innovation_score", 0),
            "traffic_score": item.get("score_data", {}).get("traffic_score", 0),
            "report_score": report_score(item),
            "rendered": id(item) in report_rendered_ids,
            "report_lane": (
                "main"
                if id(item) in rendered_ids
                else "research_watch"
                if id(item) in research_watch_ids
                else "not_rendered"
            ),
            "reason": (
                "selected_after_evidence_aware_ranking"
                if id(item) in rendered_ids
                else "moved_to_research_watch_due_to_primary_evidence_ratio"
                if id(item) in research_watch_ids
                and evidence_exclusion_reasons.get(id(item))
                == "excluded_by_primary_evidence_ratio"
                else "report_score_below_threshold"
                if report_score(item) < minimum
                else report_exclusion_reasons.get(id(item))
                or evidence_exclusion_reasons.get(id(item))
                or "eligible_but_section_capacity_exceeded"
            ),
            **(
                {"representative_link": item["_report_event_representative_link"]}
                if item.get("_report_event_representative_link")
                else {}
            ),
        }
        for item in evidence_input
    ]
    report_rendered_count = selected_count + len(strategic_watch)
    if len(selection_decisions) != len(evidence_input):
        raise AssertionError("selection decisions do not cover evidence input")
    if sum(item["rendered"] for item in selection_decisions) != report_rendered_count:
        raise AssertionError("selection decision render count does not match report")
    if any(
        item["rendered"] and item["report_score"] < minimum
        for item in selection_decisions
    ):
        raise AssertionError("sub-threshold selection decision marked as rendered")
    return ReportSelection(
        supernova=tuple(supernova),
        hardcore=tuple(hardcore),
        hype=tuple(hype),
        strategic_watch=tuple(strategic_watch),
        deep_dives=tuple(deep_dives),
        evidence_input=evidence_input,
        evidence_selection=evidence_selection,
        diagnostics={
            "selected": selected_count,
            "eligible_selected": len(selected),
            "scored_input_count": len(scored_articles),
            "completed_score_count": sum(
                not review_is_pending(article) for article in scored_articles
            ),
            "review_pending_input_count": sum(
                review_is_pending(article) for article in scored_articles
            ),
            "evidence_input_count": len(evidence_input),
            "evidence_selected": len(evidence_selection),
            "trade_evidence_eligible": evidence_trade_count,
            "trade_evidence_rejected": (
                len(evidence_selection) - evidence_trade_count
            ),
            "trade_evidence_rejection_reasons": dict(
                sorted(evidence_rejection_reasons.items())
            ),
            "evidence_input_rejection_reasons": dict(
                sorted(evidence_input_rejections.items())
            ),
            "report_score_threshold_excluded": report_score_threshold_excluded,
            "report_score_eligible_count": sum(
                report_score(item) >= minimum for item in evidence_input
            ),
            "report_score_threshold": minimum,
            "selection_decision_count": len(selection_decisions),
            "report_rendered_count": report_rendered_count,
            "main_report_selected_count": selected_count,
            "research_watch_selected_count": len(strategic_watch),
            "report_selected_count": report_rendered_count,
            "supernova": len(supernova),
            "hardcore": len(hardcore),
            "hype": len(hype),
            "strategic_watch": len(strategic_watch),
            "research_only_high_score": len(ratio_research),
            "near_hardcore": near_hardcore,
            "primary_supported": primary_supported,
            "primary_supported_ratio": (
                primary_supported / selected_count if selected_count else 0.0
            ),
            "evidence_shortfall_excluded": (
                supernova_excluded + hardcore_excluded + hype_excluded
            ),
            "discovery_only": evidence_counts["discovery_only"],
            "source_counts": dict(source_counts.most_common()),
            "leading_source": leading_source,
            "leading_source_share": (
                leading_count / selected_count if selected_count else 0.0
            ),
            "selection_decisions": selection_decisions,
        },
    )
