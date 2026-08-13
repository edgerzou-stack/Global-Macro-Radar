"""Deterministic evidence and industrial-milestone policy."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit


EVIDENCE_POLICY_VERSION = "industrial-evidence-v7-official-t1-event-cluster"

SAME_BATCH_CORROBORATION_METHOD = "same_batch_event_match_v1"
EXPLICIT_PRIMARY_URL_METHOD = "same_batch_explicit_primary_url_v1"
REGISTERED_PRIMARY_URL_METHOD = "registered_explicit_primary_url_v1"
INDEPENDENT_T1_MUTUAL_CORROBORATION_METHOD = (
    "same_batch_official_t1_event_cluster_v2"
)
OFFICIAL_T1_EVENT_CLUSTER_VERSION = "official-t1-event-cluster-v1"
_EVENT_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.IGNORECASE)
_EVENT_STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "been",
    "being",
    "but",
    "company",
    "could",
    "for",
    "from",
    "has",
    "have",
    "into",
    "its",
    "new",
    "over",
    "said",
    "says",
    "that",
    "the",
    "their",
    "this",
    "through",
    "using",
    "was",
    "were",
    "will",
    "with",
}

_PRODUCT_NAME_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*|"
    r"[A-Za-z]+[0-9][A-Za-z0-9-]*)(?![A-Za-z0-9])"
)
_PRODUCT_NAME_STOPWORDS = {
    "accelerate",
    "agent",
    "agents",
    "ai",
    "available",
    "blog",
    "blue",
    "cloud",
    "commercial",
    "customer",
    "customers",
    "deployment",
    "enterprise",
    "enterprises",
    "launch",
    "launches",
    "machine",
    "model",
    "models",
    "new",
    "news",
    "now",
    "official",
    "platform",
    "production",
    "red",
    "service",
    "services",
    "system",
    "systems",
    "technology",
}


PRIMARY_EVIDENCE_STATES = frozenset(
    {"authoritative_record", "primary_claim", "primary_supported"}
)


def _event_tokens(article):
    score = article.get("score_data") or {}
    claims = score.get("industrial_claims") or []
    text = " ".join(
        [
            str(article.get("title") or ""),
            str(article.get("summary") or ""),
            *(str(claim) for claim in claims if claim),
        ]
    ).lower()
    return {
        token
        for token in _EVENT_TOKEN.findall(text)
        if token not in _EVENT_STOPWORDS and not token.isdigit()
    }


def _published_datetime(article):
    value = str(article.get("published_at") or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_evidence_url(value):
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")
    )


def _registered_first_party_host_matches(article):
    """Bind a conditional primary claim to its registered feed origin."""
    try:
        article_host = (urlsplit(str(article.get("link") or "")).hostname or "").lower()
        feed_host = (urlsplit(str(article.get("feed_url") or "")).hostname or "").lower()
    except ValueError:
        return False
    article_host = article_host.removeprefix("www.")
    feed_host = feed_host.removeprefix("www.")
    if not article_host or not feed_host:
        return False
    return (
        article_host == feed_host
        or article_host.endswith(f".{feed_host}")
        or feed_host.endswith(f".{article_host}")
    )


def _article_host(article):
    try:
        return (
            (urlsplit(str(article.get("link") or "")).hostname or "")
            .lower()
            .removeprefix("www.")
        )
    except ValueError:
        return ""


def _normalized_identity(article, field):
    """Return an explicit legal-identity key; never infer one from a name."""
    value = article.get(field)
    if not isinstance(value, str):
        return ""
    return " ".join(value.casefold().split())


def _normalized_aliases(article, field="identity_aliases"):
    values = article.get(field)
    if not isinstance(values, list):
        return ()
    return tuple(
        normalized
        for value in values
        if isinstance(value, str)
        and (normalized := " ".join(value.casefold().split()))
    )


def _article_evidence_text(article, *, title_only=False):
    fields = ("title",) if title_only else ("title", "summary", "content")
    parts = [str(article.get(field) or "") for field in fields]
    if not title_only:
        claims = (article.get("score_data") or {}).get("industrial_claims") or []
        if isinstance(claims, list):
            parts.extend(str(claim) for claim in claims if claim)
    return "\n".join(parts)


def _contains_alias(text, alias):
    escaped = re.escape(str(alias or "").strip())
    if not escaped:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", text, re.I))


def _directly_names_both_identities(article, first_aliases, second_aliases):
    text = _article_evidence_text(article)
    return any(_contains_alias(text, alias) for alias in first_aliases) and any(
        _contains_alias(text, alias) for alias in second_aliases
    )


def _shared_unambiguous_product_name(article, other):
    """Return the one shared proper product token, or fail closed.

    Product identity is derived from titles only, after removing audited legal
    identity aliases and generic launch/deployment vocabulary.  Multiple
    remaining shared names are ambiguous and therefore cannot form a trade
    evidence cluster.
    """
    identity_terms = set()
    for candidate in (article, other):
        for field in ("identity_aliases", "audited_platform_aliases"):
            for alias in _normalized_aliases(candidate, field):
                identity_terms.update(
                    token.casefold()
                    for token in re.findall(r"[A-Za-z0-9-]+", alias)
                )

    def candidates(candidate):
        title = _article_evidence_text(candidate, title_only=True)
        found = {}
        group = []
        previous_end = None

        def flush():
            if group:
                display = " ".join(group)
                found[display.casefold()] = display
                group.clear()

        for match in _PRODUCT_NAME_TOKEN.finditer(title):
            token = match.group(0)
            normalized = token.casefold()
            separator = (
                title[previous_end : match.start()]
                if previous_end is not None
                else ""
            )
            if group and not re.fullmatch(r"[\s-]+", separator):
                flush()
            if (
                len(token) < 4
                or normalized in _PRODUCT_NAME_STOPWORDS
                or normalized in identity_terms
            ):
                flush()
            else:
                group.append(token)
            previous_end = match.end()
        flush()
        return found

    first = candidates(article)
    second = candidates(other)
    shared = sorted(set(first) & set(second))
    if len(shared) != 1 or set(first) != set(second):
        return ""
    return first[shared[0]]


def _trade_maturity(article):
    """Return the current trade milestone, or an empty string."""
    score = article.get("score_data") or {}
    text = _article_evidence_text(article)
    hardtech = classify_hardtech_milestone(text)
    result = (
        hardtech
        if hardtech["topic"] != "unrelated"
        else classify_industrial_milestone(
            text,
            event_type=score.get("event_type"),
            audited_platform_aliases=_normalized_aliases(
                article, "audited_platform_aliases"
            ),
        )
    )
    if (
        result.get("production_state") == "current"
        and result.get("milestone") in TRADE_TRIGGER_MILESTONES
    ):
        return str(result["milestone"])
    return ""


def _references_exact_article(article, other):
    other_url = _canonical_evidence_url(other.get("link"))
    return bool(other_url) and other_url in {
        _canonical_evidence_url(value)
        for value in article.get("reference_urls") or []
    }


def _current_trade_maturity(article):
    """Evaluate current maturity without depending on annotation call order."""
    return bool(_trade_maturity(article))


def _independent_mutual_t1_pair(article, other):
    """Prove two official T1 publishers independently attest one current event.

    Independence and event identity are deterministic. Exact cross-links are
    useful provenance but are not required: real first-party RSS entries often
    omit them. The pair instead needs audited legal identities, distinct
    official hosts, the same current milestone, and one unambiguous product
    proper name while both publishers are directly named in both records.
    """
    if article.get("source_tier") != "T1" or other.get("source_tier") != "T1":
        return False
    source_id = str(article.get("source_id") or "").strip()
    other_source_id = str(other.get("source_id") or "").strip()
    host = _article_host(article)
    other_host = _article_host(other)
    publisher = _normalized_identity(article, "publisher_identity")
    other_publisher = _normalized_identity(other, "publisher_identity")
    issuer = _normalized_identity(article, "issuer_identity")
    other_issuer = _normalized_identity(other, "issuer_identity")
    aliases = _normalized_aliases(article)
    other_aliases = _normalized_aliases(other)
    if not all(
        (
            source_id,
            other_source_id,
            host,
            other_host,
            publisher,
            other_publisher,
            issuer,
            other_issuer,
            aliases,
            other_aliases,
        )
    ):
        return False
    if source_id == other_source_id or host == other_host:
        return False
    # Publisher and issuer identities together represent the legal group. This
    # rejects separately branded mirrors owned by the same issuer.
    if {publisher, issuer} & {other_publisher, other_issuer}:
        return False
    if not (
        _registered_first_party_host_matches(article)
        and _registered_first_party_host_matches(other)
    ):
        return False
    if not (
        article.get("source_lane") == "evidence"
        and other.get("source_lane") == "evidence"
        and article.get("trade_eligible") == "conditional"
        and other.get("trade_eligible") == "conditional"
        and article.get("requires_corroboration") is True
        and other.get("requires_corroboration") is True
    ):
        return False
    event_type = str((article.get("score_data") or {}).get("event_type") or "")
    other_event_type = str(
        (other.get("score_data") or {}).get("event_type") or ""
    )
    if not event_type or event_type != other_event_type:
        return False
    milestone = _trade_maturity(article)
    other_milestone = _trade_maturity(other)
    if not milestone or milestone != other_milestone:
        return False
    if not (
        (article.get("score_data") or {}).get("is_relevant") is True
        and (other.get("score_data") or {}).get("is_relevant") is True
    ):
        return False
    if not (
        _directly_names_both_identities(article, aliases, other_aliases)
        and _directly_names_both_identities(other, aliases, other_aliases)
    ):
        return False
    product_name = _shared_unambiguous_product_name(article, other)
    if not product_name:
        return False
    return {
        "event_cluster_version": OFFICIAL_T1_EVENT_CLUSTER_VERSION,
        "event_product_name": product_name,
        "event_type": event_type,
        "milestone": milestone,
        "exact_reference_direction_count": int(
            _references_exact_article(article, other)
        )
        + int(_references_exact_article(other, article)),
    }


def _valid_primary_corroboration(
    article,
    *,
    allowed_tiers=frozenset({"T0", "T1"}),
    require_same_batch=False,
    require_relevant_primary=False,
    require_independent_t1=False,
):
    """Validate a concrete corroborating record, not a registry promise.

    ``registered_primary_references`` are useful discovery hints, but do not
    prove that a corroborating article was actually fetched in this run.  A T1
    conditional source therefore requires one of the two methods that bind to
    a real same-batch article.
    """
    corroboration = article.get("primary_corroboration") or {}
    allowed_methods = {
        SAME_BATCH_CORROBORATION_METHOD,
        EXPLICIT_PRIMARY_URL_METHOD,
        REGISTERED_PRIMARY_URL_METHOD,
        INDEPENDENT_T1_MUTUAL_CORROBORATION_METHOD,
    }
    if require_same_batch:
        allowed_methods.remove(REGISTERED_PRIMARY_URL_METHOD)
    valid = (
        isinstance(corroboration, dict)
        and corroboration.get("method") in allowed_methods
        and corroboration.get("primary_source_tier") in allowed_tiers
        and bool(_canonical_evidence_url(corroboration.get("primary_url")))
        and bool(str(corroboration.get("primary_title") or "").strip())
        and bool(str(corroboration.get("primary_source_id") or "").strip())
        and (
            not require_relevant_primary
            or article.get("_primary_corroboration_verified_relevant") is True
        )
    )
    if not valid:
        return False
    if (
        require_independent_t1
        and corroboration.get("primary_source_tier") == "T1"
    ):
        return (
            corroboration.get("method")
            == INDEPENDENT_T1_MUTUAL_CORROBORATION_METHOD
            and corroboration.get("event_cluster_verified") is True
            and corroboration.get("event_cluster_version")
            == OFFICIAL_T1_EVENT_CLUSTER_VERSION
            and bool(str(corroboration.get("event_cluster_id") or "").strip())
            and bool(str(corroboration.get("event_product_name") or "").strip())
            and len(corroboration.get("event_cluster_member_ids") or []) == 2
            and len(corroboration.get("event_cluster_evidence_sha256") or []) == 2
            and bool(str(corroboration.get("publisher_identity") or "").strip())
            and bool(
                str(corroboration.get("primary_publisher_identity") or "").strip()
            )
            and bool(str(corroboration.get("issuer_identity") or "").strip())
            and bool(
                str(corroboration.get("primary_issuer_identity") or "").strip()
            )
        )
    return True


def _compatible_event(primary, event_type):
    primary_event_type = (primary.get("score_data") or {}).get("event_type")
    return (
        primary_event_type == event_type
        or "other_industrial" in {primary_event_type, event_type}
    )


def _corroboration(primary, method):
    return {
        "method": method,
        "primary_url": str(primary["link"]),
        "primary_title": str(primary.get("title") or ""),
        "primary_source_id": str(
            primary.get("source_id") or primary.get("source") or ""
        ),
        "primary_source_tier": str(primary.get("source_tier") or ""),
    }


def _cluster_member_id(article):
    explicit = str(article.get("event_id") or "").strip()
    if explicit:
        return explicit
    identity = (
        _canonical_evidence_url(article.get("link"))
        or " ".join(str(article.get("title") or "").split())
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest() if identity else ""


def _cluster_evidence_sha256(article):
    payload = {
        "member_id": _cluster_member_id(article),
        "source_id": str(article.get("source_id") or ""),
        "publisher_identity": _normalized_identity(article, "publisher_identity"),
        "issuer_identity": _normalized_identity(article, "issuer_identity"),
        "published_at": str(article.get("published_at") or ""),
        "title": " ".join(str(article.get("title") or "").split()),
        "summary": " ".join(str(article.get("summary") or "").split()),
        "event_type": str((article.get("score_data") or {}).get("event_type") or ""),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _event_cluster_payload(article, primary, cluster):
    members = sorted((_cluster_member_id(article), _cluster_member_id(primary)))
    evidence_hashes = sorted(
        (_cluster_evidence_sha256(article), _cluster_evidence_sha256(primary))
    )
    identity = {
        "version": OFFICIAL_T1_EVENT_CLUSTER_VERSION,
        "event_type": cluster["event_type"],
        "milestone": cluster["milestone"],
        "event_product_name": str(cluster["event_product_name"]).casefold(),
        "member_ids": members,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **cluster,
        "event_cluster_id": hashlib.sha256(encoded).hexdigest(),
        "event_cluster_member_ids": members,
        "event_cluster_evidence_sha256": evidence_hashes,
        "event_cluster_verified": True,
    }


def _mutual_t1_corroboration(article, primary, cluster):
    payload = _corroboration(
        primary,
        INDEPENDENT_T1_MUTUAL_CORROBORATION_METHOD,
    )
    cluster_payload = _event_cluster_payload(article, primary, cluster)
    payload.update(
        {
            **cluster_payload,
            "publisher_identity": str(article["publisher_identity"]),
            "primary_publisher_identity": str(primary["publisher_identity"]),
            "issuer_identity": str(article["issuer_identity"]),
            "primary_issuer_identity": str(primary["issuer_identity"]),
        }
    )
    return payload


def _registered_reference_corroboration(reference):
    return {
        "method": REGISTERED_PRIMARY_URL_METHOD,
        "primary_url": str(reference["url"]),
        "primary_title": "文章明确引用的已登记官方原文",
        "primary_source_id": str(reference["source_id"]),
        "primary_source_tier": str(reference["source_tier"]),
    }


def attach_same_batch_primary_corroboration(articles, *, max_days=3):
    """Attach only high-confidence, unambiguous same-batch primary evidence.

    Numeric scores do not participate.  An explicit citation to an exact
    same-batch primary URL is preferred.  Otherwise a secondary article must
    share the same scored event type, publication window, at least three event
    tokens, and at least one token unique to the candidate primary source's
    current batch.  Ties fail closed instead of guessing which official item
    supports the claim.
    """
    primaries = [
        article
        for article in articles
        if article.get("source_tier") in {"T0", "T1"}
        and article.get("link")
        and (article.get("score_data") or {}).get("event_type")
        not in {None, "non_industrial"}
    ]
    primaries_by_url = defaultdict(list)
    for primary in primaries:
        canonical_url = _canonical_evidence_url(primary.get("link"))
        if canonical_url:
            primaries_by_url[canonical_url].append(primary)
    primary_tokens = {id(article): _event_tokens(article) for article in primaries}
    source_token_frequency = defaultdict(Counter)
    for primary in primaries:
        source_id = str(primary.get("source_id") or primary.get("source") or "")
        source_token_frequency[source_id].update(primary_tokens[id(primary)])

    for article in articles:
        article.pop("primary_corroboration", None)
        article.pop("_primary_corroboration_verified_relevant", None)
        article_tier = article.get("source_tier")
        if article_tier == "T0":
            continue
        # A conditional T1 source is first-party, but the registry contract
        # still requires independent authoritative corroboration before it may
        # cross into the trade lane. T0 remains preferred. A T1 peer is only
        # admitted by the stricter reciprocal-link and legal-identity path;
        # it never enters fuzzy token matching.
        allowed_primary_tiers = {"T0"} if article_tier == "T1" else {"T0", "T1"}
        score = article.get("score_data") or {}
        event_type = score.get("event_type")
        if not event_type or score.get("is_relevant") is not True:
            continue
        published = _published_datetime(article)
        if published is None:
            continue

        if article_tier == "T1":
            # Prefer an exact same-batch T0 record whenever one is present.
            # The T1-peer path is a narrowly bounded fallback, never an
            # alternative that can displace regulator-grade evidence.
            exact_t0_matches = []
            for reference_url in article.get("reference_urls") or []:
                for primary in primaries_by_url.get(
                    _canonical_evidence_url(reference_url), ()
                ):
                    primary_published = _published_datetime(primary)
                    if (
                        primary.get("source_tier") == "T0"
                        and primary_published is not None
                        and _compatible_event(primary, event_type)
                        and abs((published - primary_published).total_seconds())
                        <= max_days * 86400
                    ):
                        exact_t0_matches.append(primary)
            exact_t0_matches = list(
                {id(item): item for item in exact_t0_matches}.values()
            )
            if len(exact_t0_matches) == 1:
                article["primary_corroboration"] = _corroboration(
                    exact_t0_matches[0],
                    EXPLICIT_PRIMARY_URL_METHOD,
                )
                article["_primary_corroboration_verified_relevant"] = (
                    (exact_t0_matches[0].get("score_data") or {}).get(
                        "is_relevant"
                    )
                    is True
                )
                continue
            if exact_t0_matches:
                continue
            mutual_t1_matches = []
            for primary in primaries:
                if id(primary) == id(article):
                    continue
                primary_published = _published_datetime(primary)
                cluster = _independent_mutual_t1_pair(article, primary)
                if (
                    primary_published is None
                    or abs((published - primary_published).total_seconds())
                    > max_days * 86400
                    or not _compatible_event(primary, event_type)
                    or not cluster
                ):
                    continue
                primary_event = (primary.get("score_data") or {}).get("event_type")
                if (
                    event_type not in set(article.get("authority_for") or [])
                    or primary_event
                    not in set(primary.get("authority_for") or [])
                ):
                    continue
                mutual_t1_matches.append((primary, cluster))
            if len(mutual_t1_matches) == 1:
                primary, cluster = mutual_t1_matches[0]
                article["primary_corroboration"] = _mutual_t1_corroboration(
                    article,
                    primary,
                    cluster,
                )
                article["_primary_corroboration_verified_relevant"] = True
                continue

        explicit_matches = []
        for reference_url in article.get("reference_urls") or []:
            for primary in primaries_by_url.get(
                _canonical_evidence_url(reference_url), ()
            ):
                if id(primary) == id(article) or primary.get(
                    "source_tier"
                ) not in allowed_primary_tiers:
                    continue
                primary_published = _published_datetime(primary)
                if primary_published is None or not _compatible_event(
                    primary, event_type
                ):
                    continue
                if (
                    abs((published - primary_published).total_seconds())
                    <= max_days * 86400
                ):
                    explicit_matches.append(primary)
        explicit_matches = list({id(item): item for item in explicit_matches}.values())
        if len(explicit_matches) == 1:
            article["primary_corroboration"] = _corroboration(
                explicit_matches[0], EXPLICIT_PRIMARY_URL_METHOD
            )
            article["_primary_corroboration_verified_relevant"] = (
                (explicit_matches[0].get("score_data") or {}).get("is_relevant")
                is True
            )
            continue
        if explicit_matches:
            continue

        registered_matches = []
        for reference in article.get("registered_primary_references") or []:
            authority_for = set(reference.get("authority_for") or [])
            if event_type not in authority_for and "other_industrial" not in authority_for:
                continue
            if reference.get("source_tier") not in allowed_primary_tiers:
                continue
            if not reference.get("url") or not reference.get("source_id"):
                continue
            registered_matches.append(reference)
        registered_matches = {
            _canonical_evidence_url(item["url"]): item
            for item in registered_matches
            if _canonical_evidence_url(item["url"])
        }
        if len(registered_matches) == 1:
            article["primary_corroboration"] = (
                _registered_reference_corroboration(
                    next(iter(registered_matches.values()))
                )
            )
            article["_primary_corroboration_verified_relevant"] = False
            continue
        if registered_matches:
            continue

        tokens = _event_tokens(article)
        if len(tokens) < 3:
            continue

        matches = []
        for primary in primaries:
            if id(primary) == id(article) or primary.get(
                "source_tier"
            ) not in allowed_primary_tiers:
                continue
            if not _compatible_event(primary, event_type):
                continue
            primary_published = _published_datetime(primary)
            if primary_published is None:
                continue
            if abs((published - primary_published).total_seconds()) > max_days * 86400:
                continue
            candidate_tokens = primary_tokens[id(primary)]
            shared = tokens & candidate_tokens
            if len(shared) < 3:
                continue
            overlap = len(shared) / min(len(tokens), len(candidate_tokens))
            if overlap < 0.25:
                continue
            source_id = str(
                primary.get("source_id") or primary.get("source") or ""
            )
            distinctive = {
                token
                for token in shared
                if source_token_frequency[source_id][token] == 1
            }
            if not distinctive:
                continue
            matches.append(
                (
                    len(distinctive),
                    overlap,
                    len(shared),
                    str(primary.get("link")),
                    primary,
                )
            )
        matches.sort(reverse=True, key=lambda item: item[:4])
        if not matches:
            continue
        best = matches[0]
        if len(matches) > 1 and best[:3] == matches[1][:3]:
            continue
        primary = best[4]
        article["primary_corroboration"] = _corroboration(
            primary, SAME_BATCH_CORROBORATION_METHOD
        )
        article["_primary_corroboration_verified_relevant"] = (
            (primary.get("score_data") or {}).get("is_relevant") is True
        )
    return articles
RESEARCH_WATCH_MILESTONES = frozenset(
    {
        "engineering_test",
        "shipping",
        "mass_production",
        "pilot_production",
        "regulatory_approval",
        "qualification",
        "clinical_readout",
        "commercial_deployment",
        "capacity_expansion",
        "tapeout",
        "prototype",
        "research_result",
        "poc",
        "testing",
    }
)
DISCOVERY_WATCH_MILESTONES = frozenset(
    {
        "engineering_test",
        "shipping",
        "mass_production",
        "pilot_production",
        "regulatory_approval",
        "qualification",
        "clinical_readout",
        "capacity_expansion",
        "tapeout",
        "poc",
        "testing",
    }
)

# Only milestones that prove an already-occurring commercial or regulatory
# state may cross the report-to-trading boundary.  Earlier engineering stages
# remain useful in Research Watch, but must never rotate a portfolio.
TRADE_TRIGGER_MILESTONES = frozenset(
    {
        "shipping",
        "mass_production",
        "regulatory_approval",
        "qualification",
        "commercial_deployment",
    }
)


_CHIEF_PRODUCT_OFFICER = re.compile(
    r"\bchief\s+product\s+officer\b|首席产品官", re.IGNORECASE
)
_CPO_TOPIC = re.compile(
    r"\bCPO\b|co[- ]packaged\s+optics?|共封装光学|硅光|silicon\s+photonics?|OIO",
    re.IGNORECASE,
)


def classify_hardtech_milestone(text):
    normalized = " ".join(str(text or "").split())
    if _CHIEF_PRODUCT_OFFICER.search(normalized):
        return {"topic": "unrelated", "milestone": "none", "production_state": "none"}
    if not _CPO_TOPIC.search(normalized):
        return {"topic": "unrelated", "milestone": "none", "production_state": "none"}

    lower = normalized.lower()
    future = bool(
        re.search(
            r"planned|plans? to|expected to|will |next year|future|roadmap|"
            r"计划|预计|有望|未来|明年",
            lower,
        )
    )
    current_shipping = bool(
        re.search(r"now shipping|shipping to customers|开始出货|已出货|交付客户", lower)
    )
    current_production = bool(
        re.search(
            r"now in (?:mass )?production|entered mass production|in volume production|"
            r"已量产|正式量产|规模量产|批量生产",
            lower,
        )
    )
    ordered_patterns = (
        ("shipping", current_shipping),
        ("mass_production", current_production),
        ("pilot_production", bool(re.search(r"pilot production|small[- ]batch|试产|小批量", lower))),
        ("qualification", bool(re.search(r"qualification|qualified by|认证|验证通过", lower))),
        ("poc", bool(re.search(r"\bpoc\b|proof of concept|customer testing|客户.*测试|概念验证", lower))),
        ("testing", bool(re.search(r"under test|in testing|测试中|进入测试", lower))),
        ("tapeout", bool(re.search(r"tape[- ]?out|流片", lower))),
        ("prototype", bool(re.search(r"prototype|样机|原型", lower))),
        ("roadmap", bool(re.search(r"roadmap|路线图|future|未来", lower))),
    )
    milestone = next((name for name, matched in ordered_patterns if matched), "none")
    production_state = (
        "current"
        if current_shipping or current_production
        else "future"
        if future
        else "not_production"
        if milestone != "none"
        else "none"
    )
    return {
        "topic": "cpo_optics",
        "milestone": milestone,
        "production_state": production_state,
    }


_INDUSTRIAL_CLAUSE_BOUNDARY = re.compile(r"[.!?;。！？；\n]+")
_FUTURE_OR_NEGATED_STATE = re.compile(
    r"\b(?:planned|plans?|planning|expected|expects?|scheduled|aims?|intends?|"
    r"will|would|could|may|might|upcoming|future|roadmap)\b|"
    r"\b(?:next year|later this year|in the coming months)\b|"
    r"\b(?:not yet|not currently)\b|"
    r"计划|预计|有望|未来|明年|拟于|尚未|暂未",
    re.IGNORECASE,
)
_PRECOMMERCIAL_STATE = re.compile(
    r"\b(?:private |public |limited )?preview\b|\bbeta\b|\bpilot\b|"
    r"\bproof of concept\b|\bpoc\b|\btrial (?:use|basis|deployment)\b|"
    r"预览|测试版|试点|试用|概念验证",
    re.IGNORECASE,
)


def _industrial_clauses(text):
    return [
        " ".join(clause.split())
        for clause in _INDUSTRIAL_CLAUSE_BOUNDARY.split(str(text or ""))
        if clause.strip()
    ]


def classify_industrial_milestone(
    text,
    *,
    event_type=None,
    audited_platform_aliases=(),
):
    """Classify progress and maturity from the clause carrying the claim.

    A future roadmap elsewhere in the article must not poison an independently
    current deployment clause. Conversely, ``will be generally available`` or
    ``now available in preview`` must not be promoted by the current-looking
    words alone. Contradictory clauses fail closed as non-current.
    """
    clauses = _industrial_clauses(text)
    patterns = (
        (
            "shipping",
            r"now shipping|shipping to customers|开始出货|已出货|交付客户",
        ),
        (
            "mass_production",
            r"entered (?:mass )?production(?!\s+use)|"
            r"now in (?:mass )?production(?!\s+use)|"
            r"volume production|已量产|正式量产|规模量产|批量生产",
        ),
        (
            "pilot_production",
            r"pilot (?:line|production)|small[- ]batch|试产|小批量|中试线",
        ),
        (
            "regulatory_approval",
            r"regulator approved|regulatory approval|granted (?:accelerated )?approval|"
            r"\b(?:fda|ema) approves?\b|authorized for commercial use|"
            r"获批|批准上市|监管批准|获准商业使用",
        ),
        (
            "qualification",
            r"customer qualification|qualified by|validation complete|"
            r"客户认证|客户验证通过|认证通过",
        ),
        (
            "clinical_readout",
            r"phase [123] (?:trial )?(?:met|results)|pivotal trial|"
            r"临床.{0,8}(?:达到终点|结果|数据)",
        ),
        (
            "commercial_deployment",
            r"commercial deployment|deployed to customers|entered service|"
            r"deployed\s+(?:more than\s+|over\s+)?\d[\d,]*(?:\+)?\s+"
            r"(?:agents|workloads)\b|"
            r"generally available|"
            r"(?:now )?available to (?:(?:eligible|select|enterprise)\s+)?"
            r"(?:[a-z0-9.-]+\s+){0,3}(?:customers|users|enterprises)|"
            r"entered (?:commercial |customer )?production use|in production use|"
            r"商业部署|商业化落地|投入运营|正式可用|已向客户开放",
        ),
        (
            "capacity_expansion",
            r"capacity expansion|new (?:fab|factory|plant)|groundbreaking|"
            r"扩产|新增产能|新建.{0,8}(?:工厂|厂|产线)|开工建设",
        ),
        (
            "engineering_test",
            r"test flight|flight test|launch attempt|booster catch|"
            r"static fire|engineering test|试飞|飞行测试|发射试验|"
            r"回收试验|静态点火|工程测试",
        ),
        (
            "tapeout",
            r"tape[- ]?out|流片",
        ),
        (
            "prototype",
            r"prototype|样机|原型",
        ),
        (
            "funding",
            r"funding round|raised \$|financing round|融资|募集资金",
        ),
        (
            "policy",
            r"final rule|new regulation|policy announced|guidance issued|"
            r"正式规则|监管新规|产业政策|发布指引",
        ),
        (
            "research_result",
            r"peer[- ]reviewed|published in (?:nature|science)|researchers demonstrated|"
            r"同行评审|发表于《?(?:自然|科学)|研究团队.{0,8}(?:实现|证明)",
        ),
        (
            "roadmap",
            r"roadmap|路线图|future|未来",
        ),
    )
    milestone = "none"
    matched_clauses = []
    for name, pattern in patterns:
        matches = [clause for clause in clauses if re.search(pattern, clause, re.I)]
        if name == "commercial_deployment" and audited_platform_aliases:
            matches.extend(
                clause
                for clause in clauses
                if re.search(r"\b(?:now\s+)?available\s+(?:on|through)\b", clause, re.I)
                and any(
                    _contains_alias(clause, alias)
                    for alias in audited_platform_aliases
                )
            )
            matches = list(dict.fromkeys(matches))
        if matches:
            milestone = name
            matched_clauses = matches
            break
    current_milestones = {
        "shipping",
        "mass_production",
        "regulatory_approval",
        "qualification",
        "clinical_readout",
        "commercial_deployment",
        "capacity_expansion",
        "tapeout",
        "prototype",
        "funding",
        "policy",
        "research_result",
    }
    has_future_or_negation = any(
        _FUTURE_OR_NEGATED_STATE.search(clause) for clause in matched_clauses
    )
    has_precommercial_qualifier = any(
        _PRECOMMERCIAL_STATE.search(clause) for clause in matched_clauses
    )
    production_state = "none"
    if milestone != "none":
        if has_future_or_negation:
            production_state = "future"
        elif has_precommercial_qualifier and milestone == "commercial_deployment":
            production_state = "not_production"
        elif milestone in current_milestones:
            production_state = "current"
        else:
            production_state = "not_production"
    return {
        "milestone": milestone,
        "production_state": production_state,
    }


def trade_evidence_decision(article):
    """Return the deterministic decision for crossing into the trade lane."""
    score = article.get("score_data") or {}
    event_type = score.get("event_type")
    tier = article.get("source_tier")
    configured_trade = article.get("trade_eligible", False)
    authority_for = set(article.get("authority_for") or [])
    milestone = article.get("industrial_milestone") or "none"
    production_state = article.get("production_state") or "none"

    conditional_t1 = tier == "T1" and configured_trade == "conditional"
    if tier not in {"T0", "T1"}:
        return {"eligible": False, "reason": "authoritative_source_required"}
    if tier == "T0" and configured_trade is not True:
        return {"eligible": False, "reason": "source_not_trade_enabled"}
    if tier == "T1" and not conditional_t1:
        return {"eligible": False, "reason": "source_not_trade_enabled"}
    if conditional_t1:
        if (
            article.get("source_lane") != "evidence"
            or article.get("evidence_state") != "primary_claim"
            or not article.get("source_id")
            or article.get("requires_corroboration") is not True
            or not _registered_first_party_host_matches(article)
        ):
            return {"eligible": False, "reason": "t1_primary_binding_required"}
        if not _valid_primary_corroboration(
            article,
            allowed_tiers=frozenset({"T0", "T1"}),
            require_same_batch=True,
            require_relevant_primary=True,
            require_independent_t1=True,
        ):
            return {
                "eligible": False,
                "reason": "t1_independent_corroboration_required",
            }
    if authority_for and event_type not in authority_for:
        return {"eligible": False, "reason": "source_not_authoritative_for_event"}
    if production_state != "current":
        return {"eligible": False, "reason": "current_maturity_required"}
    if milestone not in TRADE_TRIGGER_MILESTONES:
        return {"eligible": False, "reason": "milestone_is_research_only"}
    return {
        "eligible": True,
        "reason": (
            "current_conditionally_bound_primary_trade_milestone"
            if conditional_t1
            else "current_authoritative_trade_milestone"
        ),
        "milestone": milestone,
        "production_state": production_state,
        # T1 conditional is only an event-level authorization.  The quant
        # boundary must still prove that an exact source quote names the same
        # issuer as the selected security before it may create an intent.
        "requires_direct_entity_binding": conditional_t1,
    }


def annotate_article_evidence(article):
    score = article.get("score_data") or {}
    industrial_claims = score.get("industrial_claims") or []
    text = "\n".join(
        str(article.get(field) or "")
        for field in ("title", "summary", "content")
    )
    if isinstance(industrial_claims, list):
        text = "\n".join([text, *(str(claim) for claim in industrial_claims)])
    event_type = score.get("event_type")
    hardtech = classify_hardtech_milestone(text)
    if hardtech["topic"] != "unrelated":
        strategic_topic = hardtech["topic"]
        milestone = hardtech["milestone"]
        production_state = hardtech["production_state"]
    else:
        industrial = classify_industrial_milestone(
            text,
            event_type=event_type,
            audited_platform_aliases=_normalized_aliases(
                article, "audited_platform_aliases"
            ),
        )
        milestone = industrial["milestone"]
        production_state = industrial["production_state"]
        domains = article.get("source_domains") or []
        domain = str(domains[0]) if domains else "general"
        strategic_topic = (
            f"{domain}:{event_type or 'industrial'}"
            if milestone != "none"
            else "unrelated"
        )
    article["strategic_topic"] = strategic_topic
    article["industrial_milestone"] = milestone
    article["production_state"] = production_state

    tier = article.get("source_tier")
    deep_dive = article.get("deep_dive") or {}
    has_verified_primary = (
        isinstance(deep_dive, dict)
        and deep_dive.get("evidence_mode") == "verified_primary"
        and bool(deep_dive.get("primary_url"))
    )
    has_same_batch_primary = _valid_primary_corroboration(article)
    if tier == "T0":
        evidence_state = "authoritative_record"
    elif tier == "T1":
        evidence_state = "primary_claim"
    elif has_verified_primary or has_same_batch_primary:
        evidence_state = "primary_supported"
    else:
        evidence_state = "discovery_only"
    article["evidence_state"] = evidence_state

    decision = trade_evidence_decision(article)
    article["trade_evidence_decision"] = decision
    article["trade_evidence_eligible"] = decision["eligible"]
    return article


def research_watch_decision(article):
    """Return an auditable evidence/maturity decision for the research lane.

    Numeric model scores intentionally do not participate.  Research Watch is
    for concrete but early, future, or not-yet-primary-supported industrial
    evidence; it is not a fallback bucket for articles that merely scored below
    the formal report threshold.
    """
    score = article.get("score_data") or {}
    if score.get("is_relevant") is not True:
        return {"eligible": False, "reason": "not_relevant"}
    if score.get("is_vague_or_roundup") is True:
        return {"eligible": False, "reason": "vague_or_roundup"}
    if article.get("strategic_topic") in {None, "unrelated"}:
        return {"eligible": False, "reason": "no_strategic_topic"}

    milestone = article.get("industrial_milestone")
    if milestone not in RESEARCH_WATCH_MILESTONES:
        return {"eligible": False, "reason": "unsupported_milestone"}

    evidence_state = article.get("evidence_state") or "discovery_only"
    if (
        evidence_state not in PRIMARY_EVIDENCE_STATES
        and milestone not in DISCOVERY_WATCH_MILESTONES
    ):
        return {
            "eligible": False,
            "reason": "primary_evidence_required_for_milestone",
        }

    production_state = article.get("production_state") or "none"
    if production_state == "none":
        return {"eligible": False, "reason": "unknown_maturity"}

    return {
        "eligible": True,
        "reason": "evidence_maturity_watch",
        "evidence_state": evidence_state,
        "milestone": milestone,
        "production_state": production_state,
    }


def is_concrete_strategic_hardtech(article):
    """Backward-compatible boolean wrapper for the research-lane policy."""
    return research_watch_decision(article)["eligible"]
