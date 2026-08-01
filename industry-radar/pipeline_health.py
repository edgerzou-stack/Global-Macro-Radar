import os
from datetime import date, datetime, time, timedelta, timezone


def aware_utc_timestamp(value, field_name):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                f"{field_name} must be a valid ISO timestamp"
            ) from error
    else:
        raise ValueError(
            f"{field_name} must be a timezone-aware timestamp"
        )
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def rss_reference_time_utc():
    configured = os.environ.get("MOCK_NOW_UTC")
    if configured:
        return aware_utc_timestamp(configured, "MOCK_NOW_UTC")
    return datetime.now(timezone.utc)


def validate_rss_fixture_effective_date(articles, health, effective_date):
    if not isinstance(effective_date, date):
        raise TypeError("effective_date must be a date")
    cutoff = datetime.combine(
        effective_date + timedelta(days=1),
        time.min,
        tzinfo=timezone.utc,
    )
    for index, article in enumerate(articles):
        published_at = aware_utc_timestamp(
            article.get("published_at"),
            f"articles[{index}].published_at",
        )
        if published_at >= cutoff:
            raise ValueError(
                "RSS fixture contains post-effective-date article: "
                f"{article.get('title', '<untitled>')} at "
                f"{published_at.isoformat()}"
            )
    for index, item in enumerate(health):
        newest = item.get("newest_published_at")
        if newest is None:
            continue
        newest_at = aware_utc_timestamp(
            newest,
            f"health[{index}].newest_published_at",
        )
        if newest_at >= cutoff:
            raise ValueError(
                "RSS fixture health contains post-effective-date content: "
                f"{item.get('url', '<unknown>')} at "
                f"{newest_at.isoformat()}"
            )


def validate_rss_health(
    health,
    max_failure_ratio=0.5,
    *,
    min_healthy_ratio=0.0,
    min_fresh_sources=1,
    min_total_fresh_entries=1,
    min_configured_sources=1,
    article_count=None,
    critical_source_groups=None,
    reference_time=None,
    max_fresh_entry_share=None,
):
    if not health:
        raise RuntimeError(
            "RSS health check failed: no sources were configured"
        )
    ratio_settings = {
        "max_failure_ratio": max_failure_ratio,
        "min_healthy_ratio": min_healthy_ratio,
    }
    if max_fresh_entry_share is not None:
        ratio_settings["max_fresh_entry_share"] = max_fresh_entry_share
    for name, value in ratio_settings.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric")
        if not 0 <= float(value) <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    minimum_settings = {
        "min_fresh_sources": min_fresh_sources,
        "min_total_fresh_entries": min_total_fresh_entries,
        "min_configured_sources": min_configured_sources,
    }
    for name, value in minimum_settings.items():
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    failed = [item for item in health if item.get("status") == "failed"]
    healthy = [item for item in health if item.get("status") == "healthy"]
    fresh = [
        item
        for item in health
        if item.get("status") != "failed"
        and int(item.get("fresh_entries") or 0) > 0
    ]
    total_fresh_entries = sum(
        int(item.get("fresh_entries") or 0) for item in health
    )
    leading_source = max(
        health,
        key=lambda item: int(item.get("fresh_entries") or 0),
    )
    leading_source_entries = int(
        leading_source.get("fresh_entries") or 0
    )
    leading_source_share = (
        leading_source_entries / total_fresh_entries
        if total_fresh_entries
        else 0.0
    )
    failure_ratio = len(failed) / len(health)
    healthy_ratio = len(healthy) / len(health)
    reasons = []
    critical_group_summaries = []
    reference_time = aware_utc_timestamp(
        reference_time or datetime.now(timezone.utc),
        "reference_time",
    )

    if len(health) < min_configured_sources:
        reasons.append(
            f"configured sources {len(health)} below minimum "
            f"{min_configured_sources}"
        )
    if failure_ratio > max_failure_ratio:
        reasons.append(
            f"failed sources {len(failed)}/{len(health)} exceed "
            f"{max_failure_ratio:.0%}"
        )
    if healthy_ratio < min_healthy_ratio:
        reasons.append(
            f"healthy sources {len(healthy)}/{len(health)} below "
            f"{min_healthy_ratio:.0%}"
        )
    if len(fresh) < min_fresh_sources:
        reasons.append(
            f"fresh sources {len(fresh)} below minimum {min_fresh_sources}"
        )
    if total_fresh_entries < min_total_fresh_entries:
        reasons.append(
            f"fresh entries {total_fresh_entries} below minimum "
            f"{min_total_fresh_entries}"
        )
    if article_count is not None and article_count != total_fresh_entries:
        reasons.append(
            f"article count {article_count} does not match source total "
            f"{total_fresh_entries}"
        )

    groups = [] if critical_source_groups is None else critical_source_groups
    if not isinstance(groups, list):
        raise ValueError("critical_source_groups must be a list")
    health_by_url = {str(item.get("url")): item for item in health}
    seen_names = set()
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(
                f"critical_source_groups[{index}] must be an object"
            )
        name = group.get("name")
        sources = group.get("sources")
        if (
            not isinstance(name, str)
            or not name.strip()
            or name in seen_names
        ):
            raise ValueError(
                f"critical_source_groups[{index}] has invalid/duplicate name"
            )
        seen_names.add(name)
        if (
            not isinstance(sources, list)
            or not sources
            or any(
                not isinstance(url, str) or not url.strip()
                for url in sources
            )
            or len(sources) != len(set(sources))
        ):
            raise ValueError(
                f"critical source group {name} must contain unique source URLs"
            )
        legacy_healthy_gate = "min_available_sources" not in group
        legacy_fresh_gate = "min_current_sources" not in group
        min_available = group.get(
            "min_available_sources",
            group.get("min_healthy_sources", 1),
        )
        min_current = group.get(
            "min_current_sources",
            group.get("min_fresh_sources", 1),
        )
        for setting_name, value in (
            ("min_available_sources", min_available),
            ("min_current_sources", min_current),
        ):
            if type(value) is not int or not 0 <= value <= len(sources):
                raise ValueError(
                    f"critical source group {name} {setting_name} must be "
                    f"between 0 and {len(sources)}"
                )
        maximum_age_hours = group.get("content_max_age_hours")
        if maximum_age_hours is not None and (
            isinstance(maximum_age_hours, bool)
            or not isinstance(maximum_age_hours, (int, float))
            or maximum_age_hours <= 0
        ):
            raise ValueError(
                f"critical source group {name} content_max_age_hours "
                "must be a positive number"
            )

        group_health = [
            health_by_url[url] for url in sources if url in health_by_url
        ]
        missing = [url for url in sources if url not in health_by_url]
        group_healthy = sum(
            item.get("status") == "healthy" for item in group_health
        )
        group_fresh = sum(
            item.get("status") != "failed"
            and int(item.get("fresh_entries") or 0) > 0
            for item in group_health
        )
        group_available = sum(
            item.get("status") != "failed"
            and int(item.get("total_entries") or 0) > 0
            for item in group_health
        )
        if maximum_age_hours is None:
            group_current = group_fresh
        else:
            group_current = 0
            maximum_age = timedelta(hours=float(maximum_age_hours))
            for item in group_health:
                if item.get("status") == "failed":
                    continue
                newest = item.get("newest_published_at")
                if not newest:
                    continue
                newest_at = aware_utc_timestamp(
                    newest,
                    f"critical source group {name} newest_published_at",
                )
                age = reference_time - newest_at
                if -timedelta(minutes=5) <= age <= maximum_age:
                    group_current += 1
        if missing:
            reasons.append(
                f"critical source group {name} missing {len(missing)} "
                "configured sources"
            )
        availability_count = (
            group_healthy if legacy_healthy_gate else group_available
        )
        availability_label = (
            "healthy sources"
            if legacy_healthy_gate
            else "available sources"
        )
        current_count = group_fresh if legacy_fresh_gate else group_current
        current_label = (
            "fresh sources" if legacy_fresh_gate else "current sources"
        )
        if availability_count < min_available:
            reasons.append(
                f"critical source group {name} {availability_label} "
                f"{availability_count} below minimum {min_available}"
            )
        if current_count < min_current:
            reasons.append(
                f"critical source group {name} {current_label} "
                f"{current_count} below minimum {min_current}"
            )
        critical_group_summaries.append(
            {
                "name": name,
                "configured_sources": len(group_health),
                "expected_sources": len(sources),
                "available_sources": group_available,
                "current_sources": group_current,
                "content_max_age_hours": (
                    float(maximum_age_hours)
                    if maximum_age_hours is not None
                    else None
                ),
                "healthy_sources": group_healthy,
                "fresh_sources": group_fresh,
            }
        )
    if reasons:
        raise RuntimeError(
            "RSS health check failed: " + "; ".join(reasons)
        )
    return {
        "configured_sources": len(health),
        "failed_sources": len(failed),
        "healthy_sources": len(healthy),
        "fresh_sources": len(fresh),
        "total_fresh_entries": total_fresh_entries,
        "failure_ratio": failure_ratio,
        "healthy_ratio": healthy_ratio,
        "leading_fresh_source": str(leading_source.get("url") or ""),
        "leading_fresh_source_entries": leading_source_entries,
        "leading_fresh_source_share": leading_source_share,
        "source_concentration_warning": bool(
            max_fresh_entry_share is not None
            and leading_source_share > float(max_fresh_entry_share)
        ),
        "critical_source_groups": critical_group_summaries,
    }
