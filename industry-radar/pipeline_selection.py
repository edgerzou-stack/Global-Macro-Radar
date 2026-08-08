from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from collections import Counter

from evidence_policy import annotate_article_evidence, research_watch_decision
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
    supernova.sort(
        key=lambda item: item["score_data"].get("innovation_score", 0)
        + item["score_data"].get("traffic_score", 0),
        reverse=True,
    )
    hardcore.sort(
        key=lambda item: item["score_data"].get("innovation_score", 0),
        reverse=True,
    )
    hype.sort(
        key=lambda item: item["score_data"].get("traffic_score", 0),
        reverse=True,
    )
    strategic_candidates.sort(
        key=lambda item: item["score_data"].get("innovation_score", 0),
        reverse=True,
    )
    strategic_watch = []
    per_topic = Counter()
    max_per_topic = strategic_config.get("max_items_per_topic", 2)
    if type(max_per_topic) is not int or max_per_topic <= 0:
        raise ValueError("max_items_per_topic must be a positive integer")
    for article in strategic_candidates:
        topic = str(article.get("strategic_topic") or "unknown")
        if per_topic[topic] >= max_per_topic:
            continue
        per_topic[topic] += 1
        strategic_watch.append(article)
    source_counts = Counter(
        str(item.get("source") or "unknown") for item in selected
    )
    leading_source, leading_count = (
        source_counts.most_common(1)[0] if source_counts else ("", 0)
    )
    selected_count = len(selected)
    evidence_counts = Counter(
        str(item.get("evidence_state") or "discovery_only") for item in selected
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
        for item in selected
    )
    return ReportSelection(
        supernova=tuple(supernova[:10]),
        hardcore=tuple(hardcore[:10]),
        hype=tuple(hype[:10]),
        strategic_watch=tuple(strategic_watch[:10]),
        deep_dives=tuple(deep_dives),
        diagnostics={
            "selected": selected_count,
            "supernova": len(supernova),
            "hardcore": len(hardcore),
            "hype": len(hype),
            "strategic_watch": len(strategic_watch),
            "near_hardcore": near_hardcore,
            "primary_supported": primary_supported,
            "primary_supported_ratio": (
                primary_supported / selected_count if selected_count else 0.0
            ),
            "discovery_only": evidence_counts["discovery_only"],
            "source_counts": dict(source_counts.most_common()),
            "leading_source": leading_source,
            "leading_source_share": (
                leading_count / selected_count if selected_count else 0.0
            ),
        },
    )
