from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from collections import Counter


@dataclass(frozen=True)
class ReportSelection:
    supernova: tuple
    hardcore: tuple
    hype: tuple
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
        link = article["link"]
        title = article["title"].lower()
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
    lookback_days = config.get("output", {}).get(
        "report_days_lookback",
        2,
    )
    date_text = report_date.isoformat()
    cutoff = (report_date - timedelta(days=lookback_days)).isoformat()
    selected = []
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
        if innovation >= minimum or traffic >= minimum:
            selected.append(article)
    if selected and deduplicate:
        selected = deduplicator(selected, config)

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
    source_counts = Counter(
        str(item.get("source") or "unknown") for item in selected
    )
    leading_source, leading_count = (
        source_counts.most_common(1)[0] if source_counts else ("", 0)
    )
    selected_count = len(selected)
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
        deep_dives=tuple(deep_dives),
        diagnostics={
            "selected": selected_count,
            "supernova": len(supernova),
            "hardcore": len(hardcore),
            "hype": len(hype),
            "near_hardcore": near_hardcore,
            "source_counts": dict(source_counts.most_common()),
            "leading_source": leading_source,
            "leading_source_share": (
                leading_count / selected_count if selected_count else 0.0
            ),
        },
    )
