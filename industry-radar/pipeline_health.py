import os
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlsplit


def _publisher_host_key(item):
    host = (urlsplit(str(item.get("url") or "")).hostname or "").lower().rstrip(".")
    # Without a public-suffix dependency, collapse to the last two labels.
    # This deliberately undercounts e.g. co.uk publishers, never treats sibling
    # feeds/subdomains as independent. Registry identity resolves ambiguity.
    return "host:" + ".".join(host.split(".")[-2:])


def _discovery_domain_coverage(health, reference_time, cadence_hours):
    """Diagnostic only: alternatives never erase endpoint failure or gates."""
    discovery = [row for row in health if row.get("source_lane") == "discovery"]
    # Join owner identities across hosts AND sibling feeds with incomplete
    # metadata. A missing publisher identity must not fabricate independence.
    parents = {}

    def root(key):
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = root(parents[key])
        return parents[key]

    for row in discovery:
        host_key = _publisher_host_key(row)
        identity = str(row.get("publisher_identity") or "").strip().casefold()
        if identity:
            parents[root(host_key)] = root("publisher:" + identity)

    def publisher_key(row):
        return root(_publisher_host_key(row))

    domains = sorted({domain for row in discovery for domain in row.get("source_domains", [])})
    summaries = []
    for domain in domains:
        sources = [row for row in discovery if domain in row.get("source_domains", [])]
        failed = [row for row in sources if row.get("status") == "failed"]
        reachable = [row for row in sources if row.get("status") != "failed"]
        fresh = [row for row in reachable if int(row.get("fresh_entries") or 0) > 0]
        current = []
        for row in reachable:
            newest = row.get("newest_published_at")
            maximum_hours = cadence_hours.get(row.get("expected_cadence", "irregular"))
            if maximum_hours is None:
                # An irregular publication has no freshness SLA; do not claim
                # currency solely because an old feed remains reachable.
                if row in fresh:
                    current.append(row)
            elif newest:
                age = reference_time - aware_utc_timestamp(newest, "discovery newest_published_at")
                if -timedelta(minutes=5) <= age <= timedelta(hours=maximum_hours):
                    current.append(row)
        failed_publishers = {publisher_key(row) for row in failed}
        fresh_publishers = {publisher_key(row) for row in fresh}
        alternatives = fresh_publishers - failed_publishers
        summaries.append({
            "domain": domain,
            "configured_sources": len(sources),
            "failed_sources": len(failed),
            "reachable_sources": len(reachable),
            "current_sources": len(current),
            "fresh_sources": len(fresh),
            "fresh_entries": sum(int(row.get("fresh_entries") or 0) for row in fresh),
            "reachable_publishers": len({publisher_key(row) for row in reachable}),
            "current_publishers": len({publisher_key(row) for row in current}),
            "fresh_publishers": len(fresh_publishers),
            "fresh_independent_alternative_publishers": len(alternatives) if failed else 0,
            "failed_source_urls": sorted(str(row.get("url") or "") for row in failed),
            "failure_coverage_status": (
                "no_failed_sources" if not failed else
                "covered_by_fresh_independent_alternative" if alternatives else
                "uncovered_no_fresh_independent_alternative"
            ),
        })
    return summaries


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


def validate_rss_capture_time(articles, health, reference_time):
    """Live capture and replay share ingestion's five-minute clock tolerance."""
    from ingest import MAX_FUTURE_SKEW

    for rows, field in ((articles, "published_at"), (health, "newest_published_at")):
        for row in rows:
            if field == "newest_published_at" and row.get(field) is None:
                continue
            if aware_utc_timestamp(row.get(field), field) > reference_time + MAX_FUTURE_SKEW:
                raise ValueError(f"RSS fixture contains post-capture content: {row.get('title', row.get('url'))}")


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
    min_available_ratio=0.0,
    min_healthy_ratio=None,
    min_fresh_sources=1,
    min_total_fresh_entries=1,
    min_configured_sources=1,
    article_count=None,
    critical_source_groups=None,
    reference_time=None,
    max_fresh_entry_share=None,
    min_primary_available_sources=0,
    min_primary_fresh_entry_share=0.0,
    required_primary_domains=None,
    min_primary_available_per_domain=0,
    min_primary_current_per_domain=0,
):
    if not health:
        raise RuntimeError(
            "RSS health check failed: no sources were configured"
        )
    if min_healthy_ratio is not None:
        if min_available_ratio != 0.0:
            raise ValueError(
                "min_healthy_ratio and min_available_ratio cannot both be set"
            )
        # Backward-compatible migration: the former gate accidentally mixed
        # source reachability with publication freshness. Interpret it as the
        # availability ratio; fresh-source and fresh-entry gates remain
        # independent.
        min_available_ratio = min_healthy_ratio
    ratio_settings = {
        "max_failure_ratio": max_failure_ratio,
        "min_available_ratio": min_available_ratio,
    }
    if max_fresh_entry_share is not None:
        ratio_settings["max_fresh_entry_share"] = max_fresh_entry_share
    ratio_settings["min_primary_fresh_entry_share"] = (
        min_primary_fresh_entry_share
    )
    for name, value in ratio_settings.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric")
        if not 0 <= float(value) <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    minimum_settings = {
        "min_fresh_sources": min_fresh_sources,
        "min_total_fresh_entries": min_total_fresh_entries,
        "min_configured_sources": min_configured_sources,
        "min_primary_available_sources": min_primary_available_sources,
        "min_primary_available_per_domain": min_primary_available_per_domain,
        "min_primary_current_per_domain": min_primary_current_per_domain,
    }
    for name, value in minimum_settings.items():
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    failed = [item for item in health if item.get("status") == "failed"]
    degraded = [item for item in health if item.get("status") == "degraded"]
    healthy = [item for item in health if item.get("status") == "healthy"]
    available = [
        item
        for item in health
        if item.get("status") != "failed"
        and int(
            item.get("total_entries")
            if item.get("total_entries") is not None
            else item.get("fresh_entries")
            or 0
        )
        > 0
    ]
    fresh = [
        item
        for item in health
        if item.get("status") != "failed"
        and int(item.get("fresh_entries") or 0) > 0
    ]
    quiet = [
        item
        for item in available
        if int(item.get("fresh_entries") or 0) == 0
    ]
    parse_degraded = [
        item
        for item in degraded
        if bool(item.get("bozo"))
        or int(item.get("quarantined_entries") or 0) > 0
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
    available_ratio = len(available) / len(health)
    primary = [
        item for item in health if item.get("source_tier") in {"T0", "T1"}
    ]
    primary_available = [
        item for item in primary if item.get("status") != "failed"
    ]
    primary_failed = [
        item for item in primary if item.get("status") == "failed"
    ]
    primary_fresh_entries = sum(
        int(item.get("fresh_entries") or 0) for item in primary
    )
    primary_fresh_entry_share = (
        primary_fresh_entries / total_fresh_entries
        if total_fresh_entries
        else 0.0
    )
    reasons = []
    critical_group_summaries = []
    reference_time = aware_utc_timestamp(
        reference_time or datetime.now(timezone.utc),
        "reference_time",
    )
    required_domains = (
        [] if required_primary_domains is None else required_primary_domains
    )
    if (
        not isinstance(required_domains, list)
        or any(not isinstance(domain, str) or not domain.strip() for domain in required_domains)
        or len(required_domains) != len(set(required_domains))
    ):
        raise ValueError("required_primary_domains must contain unique domain names")

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
    if available_ratio < min_available_ratio:
        reasons.append(
            f"available sources {len(available)}/{len(health)} below "
            f"{min_available_ratio:.0%}"
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
    if len(primary_available) < min_primary_available_sources:
        reasons.append(
            f"primary available sources {len(primary_available)} below minimum "
            f"{min_primary_available_sources}"
        )
    if article_count is not None and article_count != total_fresh_entries:
        reasons.append(
            f"article count {article_count} does not match source total "
            f"{total_fresh_entries}"
        )

    cadence_hours = {
        "daily": 72.0,
        "weekly": 336.0,
        "monthly": 1080.0,
        "irregular": None,
    }
    discovery_domain_coverage = _discovery_domain_coverage(
        health, reference_time, cadence_hours
    )
    primary_domain_coverage = []
    for domain in required_domains:
        domain_sources = [
            item
            for item in primary
            if domain in (item.get("source_domains") or [])
        ]
        available_sources = [
            item for item in domain_sources if item.get("status") != "failed"
        ]
        current_sources = []
        for item in available_sources:
            newest = item.get("newest_published_at")
            cadence = item.get("expected_cadence", "irregular")
            maximum_hours = cadence_hours.get(cadence)
            if maximum_hours is None:
                if int(item.get("total_entries") or 0) > 0:
                    current_sources.append(item)
                continue
            if not newest:
                continue
            newest_at = aware_utc_timestamp(
                newest,
                f"primary domain {domain} newest_published_at",
            )
            age = reference_time - newest_at
            if -timedelta(minutes=5) <= age <= timedelta(hours=maximum_hours):
                current_sources.append(item)
        if len(available_sources) < min_primary_available_per_domain:
            reasons.append(
                f"primary domain {domain} available sources "
                f"{len(available_sources)} below minimum "
                f"{min_primary_available_per_domain}"
            )
        if len(current_sources) < min_primary_current_per_domain:
            reasons.append(
                f"primary domain {domain} current sources "
                f"{len(current_sources)} below minimum "
                f"{min_primary_current_per_domain}"
            )
        primary_domain_coverage.append(
            {
                "domain": domain,
                "configured_sources": len(domain_sources),
                "available_sources": len(available_sources),
                "current_sources": len(current_sources),
                "fresh_entries": sum(
                    int(item.get("fresh_entries") or 0)
                    for item in domain_sources
                ),
            }
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
        "available_sources": len(available),
        "degraded_sources": len(degraded),
        "quiet_sources": len(quiet),
        "parse_degraded_sources": len(parse_degraded),
        "healthy_sources": len(healthy),
        "fresh_sources": len(fresh),
        "total_fresh_entries": total_fresh_entries,
        "failure_ratio": failure_ratio,
        "available_ratio": available_ratio,
        "healthy_ratio": healthy_ratio,
        "leading_fresh_source": str(leading_source.get("url") or ""),
        "leading_fresh_source_entries": leading_source_entries,
        "leading_fresh_source_share": leading_source_share,
        "source_concentration_warning": bool(
            max_fresh_entry_share is not None
            and leading_source_share > float(max_fresh_entry_share)
        ),
        "primary_configured_sources": len(primary),
        "primary_available_sources": len(primary_available),
        "primary_fresh_entries": primary_fresh_entries,
        "primary_fresh_entry_share": primary_fresh_entry_share,
        "primary_article_mix_warning": bool(
            primary
            and total_fresh_entries
            and primary_fresh_entry_share < min_primary_fresh_entry_share
        ),
        # Source reachability and publication volume are different dimensions.
        # Keep this compatibility field aligned with actual source coverage;
        # high-volume T2 feeds must not make reachable T0/T1 feeds look broken.
        "primary_source_coverage_warning": bool(primary_failed),
        "primary_coverage_warning": bool(primary_failed),
        "critical_source_groups": critical_group_summaries,
        "primary_domain_coverage": primary_domain_coverage,
        "discovery_domain_coverage": discovery_domain_coverage,
        "discovery_uncovered_failure_domains": [
            row["domain"] for row in discovery_domain_coverage
            if row["failure_coverage_status"] == "uncovered_no_fresh_independent_alternative"
        ],
    }
