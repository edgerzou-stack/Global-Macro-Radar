from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from collections import Counter
import math

from evidence_policy import (
    annotate_article_evidence,
    attach_same_batch_primary_corroboration,
    research_watch_decision,
)
from url_identity import canonicalize_article_url


@dataclass(frozen=True)
class ReportSelection:
    supernova: tuple
    hardcore: tuple
    hype: tuple
    strategic_watch: tuple
    deep_dives: tuple
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
    minimum = config.get("output", {}).get("min_score_to_keep", 6)
    strategic_config = config.get("strategic_hardtech", {})
    strategic_enabled = strategic_config.get("enabled") is True
    lookback_days = config.get("output", {}).get(
        "report_days_lookback",
        2,
    )
    date_text = report_date.isoformat()
    cutoff = (report_date - timedelta(days=lookback_days)).isoformat()
    attach_same_batch_primary_corroboration(scored_articles)
    selected = []
    strategic_candidates = []
    for article in scored_articles:
        published = article.get("published_at", "")[:10]
        if published and (published < cutoff or published > date_text):
            continue
        score = relevance_gate(article, article.get("score_data", {}))
        article["score_data"] = score
        if not score.get("is_relevant"):
            continue
        score = dict(score)
        innovation = normalize_score(score.get("innovation_score", 0))
        traffic = normalize_score(score.get("traffic_score", 0))
        score["innovation_score"] = innovation
        score["traffic_score"] = traffic
        article["score_data"] = score
        annotate_article_evidence(article)
        if innovation >= minimum or traffic >= minimum:
            selected.append(article)
        elif strategic_enabled:
            watch_decision = research_watch_decision(article)
            article["research_watch_decision"] = watch_decision
            if watch_decision["eligible"]:
                strategic_candidates.append(article)
    if selected and deduplicate:
        selected = deduplicator(selected, config)

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
        for article in selected:
            if article.get("source_lane") == "discovery":
                source_id = str(
                    article.get("source_id") or article.get("source") or "unknown"
                )
                if discovery_counts[source_id] >= max_discovery_per_source:
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
    strategic_candidates.sort(
        key=lambda item: _stable_rank_key(
            item,
            lambda candidate: candidate["score_data"].get(
                "innovation_score", 0
            ),
        ),
    )
    strategic_watch = list(ratio_research[:max_items_per_section])
    strategic_watch_ids = {id(item) for item in strategic_watch}
    per_topic = Counter()
    max_per_topic = strategic_config.get("max_items_per_topic", 2)
    if type(max_per_topic) is not int or max_per_topic <= 0:
        raise ValueError("max_items_per_topic must be a positive integer")
    for article in strategic_candidates:
        if id(article) in strategic_watch_ids:
            continue
        topic = str(article.get("strategic_topic") or "unknown")
        if per_topic[topic] >= max_per_topic:
            continue
        per_topic[topic] += 1
        strategic_watch.append(article)
        strategic_watch_ids.add(id(article))
    strategic_watch = strategic_watch[:max_items_per_section]
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
    selection_decisions = [
        {
            "title": str(item.get("title") or ""),
            "link": str(item.get("link") or ""),
            "source": str(item.get("source") or item.get("source_id") or "unknown"),
            "evidence_state": str(item.get("evidence_state") or "discovery_only"),
            "innovation_score": item.get("score_data", {}).get("innovation_score", 0),
            "traffic_score": item.get("score_data", {}).get("traffic_score", 0),
            "rendered": id(item) in rendered_ids or id(item) in research_watch_ids,
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
                else evidence_exclusion_reasons.get(
                    id(item), "eligible_but_section_capacity_exceeded"
                )
            ),
        }
        for item in selected
    ]
    return ReportSelection(
        supernova=tuple(supernova),
        hardcore=tuple(hardcore),
        hype=tuple(hype),
        strategic_watch=tuple(strategic_watch),
        deep_dives=tuple(deep_dives),
        diagnostics={
            "selected": selected_count,
            "eligible_selected": len(selected),
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
