"""Deterministic evidence and industrial-milestone policy."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from evidence_milestones import (
    TRADE_TRIGGER_MILESTONES,
    _article_evidence_text,
    _classify_article_industrial_milestone,
    _contains_alias,
    _normalized_aliases,
    _normalized_identity,
    classify_hardtech_milestone,
    classify_industrial_milestone,
)

EVIDENCE_POLICY_VERSION = "industrial-evidence-v10-spacex-event-bound"
# Downstream annotation identity, not the frozen human scoring rubric.
MILESTONE_CLASSIFIER_VERSION = "article-event-attribution-v1"

SAME_BATCH_CORROBORATION_METHOD = "same_batch_event_match_v1"
EXPLICIT_PRIMARY_URL_METHOD = "same_batch_explicit_primary_url_v1"
REGISTERED_PRIMARY_URL_METHOD = "registered_explicit_primary_url_v1"
INDEPENDENT_T1_MUTUAL_CORROBORATION_METHOD = (
    "same_batch_official_t1_event_cluster_v2"
)
REGULATOR_ISSUER_CORROBORATION_METHOD = (
    "same_batch_regulator_issuer_event_cluster_v1"
)
OFFICIAL_T1_EVENT_CLUSTER_VERSION = "official-t1-event-cluster-v1"
REGULATOR_ISSUER_EVENT_CLUSTER_VERSION = "regulator-issuer-event-cluster-v1"
REGULATORY_SOURCE_IDS = frozenset({"fda_press_releases", "ema_news"})
SPACEX_SOURCE_ID = "spacex_updates"
SPACEX_UPDATES_API_URL = (
    "https://content.spacex.com/api/spacex-website/updates"
)
SPACEX_CANONICAL_HOST = "www.spacex.com"
SPACEX_CANONICAL_PATH = re.compile(
    r"/updates/[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?/"
)
SPACEX_DOCUMENT_ID = re.compile(r"[a-z0-9]{8,64}")
SPACEX_UNSCORED_PLACEHOLDER_CONTRACT = "unscored-placeholder-v1"
SPACEX_ALLOWED_DEFERRED_REASON_BY_RESOLUTION = {
    "unscored": frozenset(
        {"api_disabled_or_budget", "batch_budget_exhausted"}
    ),
    "manual": frozenset({"interactive_manual_pending"}),
}
SPACEX_EVENT_ENTITY = re.compile(
    r"\b(?:space\s*x|spacex|starship|super\s+heavy|starlink|"
    r"falcon\s+(?:9|heavy)|(?:crew|cargo)\s+dragon|dragon\s+spacecraft|"
    r"raptor\s+engine)\b",
    re.IGNORECASE,
)
SPACEX_VEHICLE_PATTERNS = {
    "starship": re.compile(r"\bstarship\b", re.IGNORECASE),
    "super-heavy": re.compile(r"\bsuper\s+heavy\b", re.IGNORECASE),
    "starlink": re.compile(r"\bstarlink\b", re.IGNORECASE),
    "falcon-9": re.compile(r"\bfalcon\s*9\b", re.IGNORECASE),
    "falcon-heavy": re.compile(r"\bfalcon\s+heavy\b", re.IGNORECASE),
    "crew-dragon": re.compile(r"\bcrew\s+dragon\b", re.IGNORECASE),
    "cargo-dragon": re.compile(r"\bcargo\s+dragon\b", re.IGNORECASE),
    "dragon-spacecraft": re.compile(
        r"\bdragon\s+(?:spacecraft|capsule)\b", re.IGNORECASE
    ),
    "raptor": re.compile(r"\braptor(?:\s+engine)?\b", re.IGNORECASE),
}
SPACEX_EVENT_ACTION_PATTERNS = {
    "flight-test": re.compile(r"\b(?:flight\s+test|test\s+flight)\b", re.I),
    "static-fire": re.compile(r"\bstatic\s+fire\b", re.I),
    "launch": re.compile(r"\b(?:launch(?:ed|es|ing)?|lift[- ]?off)\b", re.I),
    "deployment": re.compile(r"\bdeploy(?:ed|ment|ing)?\b", re.I),
    "recovery": re.compile(
        r"\b(?:recover(?:ed|y|ing)?|booster\s+catch|landed|landing)\b",
        re.I,
    ),
    "splashdown": re.compile(r"\bsplashdown\b", re.I),
    "anomaly": re.compile(r"\b(?:anomaly|explod(?:e|ed|es|ing)|failure)\b", re.I),
}
SPACEX_MISSION_ID = re.compile(
    r"\b(?:crew|crs|ax|ussf|transporter|bandwagon|starlink)[-\s]?[0-9]+"
    r"(?:[-\s][0-9]+)?\b|\bflight\s+(?:test\s+)?(?:number\s+|no\.?\s*)?[0-9]+\b",
    re.IGNORECASE,
)
REGULATORY_ENTITY_ROLE_PATTERN = re.compile(
    r"\b(?:applicant|application holder|sponsor|marketing authori[sz]ation holder|"
    r"rights holder|licensed to|manufactured by|marketed by|developed by|"
    r"submitted by|owned by|granted.{0,80}approval.{0,80}to)\b|"
    r"(?:申请人|申办方|上市许可持有人|权利人|"
    r"生产企业|由.{0,40}(?:研发|生产|持有))",
    re.IGNORECASE,
)
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
    audited_hosts = article.get("official_hosts")
    if isinstance(audited_hosts, list) and audited_hosts:
        return feed_host in audited_hosts and article_host in audited_hosts
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
        else _classify_article_industrial_milestone(article)
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


def _regulator_issuer_pair(article, regulator):
    """Prove that a regulator and issuer independently describe one approval."""
    if article.get("source_tier") != "T1" or regulator.get("source_tier") != "T0":
        return False
    if str(regulator.get("source_id") or "") not in REGULATORY_SOURCE_IDS:
        return False
    aliases = _normalized_aliases(article)
    if not aliases or not _registered_first_party_host_matches(article):
        return False
    if not (
        article.get("source_lane") == "evidence"
        and article.get("trade_eligible") == "conditional"
        and article.get("requires_corroboration") is True
        and regulator.get("source_lane") == "evidence"
        and regulator.get("trade_eligible") is True
    ):
        return False
    event_type = str((article.get("score_data") or {}).get("event_type") or "")
    regulator_event_type = str(
        (regulator.get("score_data") or {}).get("event_type") or ""
    )
    if not event_type or event_type != regulator_event_type:
        return False
    if not (
        event_type in set(article.get("authority_for") or [])
        and event_type in set(regulator.get("authority_for") or [])
    ):
        return False
    if _trade_maturity(article) != "regulatory_approval" or _trade_maturity(
        regulator
    ) != "regulatory_approval":
        return False
    if not (
        (article.get("score_data") or {}).get("is_relevant") is True
        and (regulator.get("score_data") or {}).get("is_relevant") is True
    ):
        return False
    issuer_text = _article_evidence_text(article)
    regulator_text = _article_evidence_text(regulator)
    if not (
        any(_contains_alias(issuer_text, alias) for alias in aliases)
        and any(_contains_alias(regulator_text, alias) for alias in aliases)
        and REGULATORY_ENTITY_ROLE_PATTERN.search(regulator_text)
    ):
        return False
    product_name = _shared_unambiguous_product_name(article, regulator)
    if not product_name:
        return False
    return {
        "event_cluster_version": REGULATOR_ISSUER_EVENT_CLUSTER_VERSION,
        "event_product_name": product_name,
        "event_type": event_type,
        "milestone": "regulatory_approval",
        "exact_reference_direction_count": int(
            _references_exact_article(article, regulator)
        )
        + int(_references_exact_article(regulator, article)),
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
        REGULATOR_ISSUER_CORROBORATION_METHOD,
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


def _strict_spacex_primary(article):
    """Accept only a record emitted by the audited SpaceX update adapter."""
    if (
        str(article.get("source_id") or "") != SPACEX_SOURCE_ID
        or article.get("source_tier") != "T1"
        or article.get("source_lane") != "evidence"
        or _normalized_identity(article, "publisher_identity") != "spacex"
        or _normalized_identity(article, "issuer_identity") != "spacex"
        or article.get("canonical_verification") != "official_api_update_id"
        or not SPACEX_DOCUMENT_ID.fullmatch(
            str(article.get("official_document_id") or "")
        )
        or _canonical_evidence_url(article.get("feed_url"))
        != SPACEX_UPDATES_API_URL
    ):
        return False
    try:
        parsed = urlsplit(str(article.get("link") or "").strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == SPACEX_CANONICAL_HOST
        and parsed.username is None
        and parsed.password is None
        and port is None
        and not parsed.query
        and not parsed.fragment
        and bool(SPACEX_CANONICAL_PATH.fullmatch(parsed.path))
    )


def _spacex_event_text(article):
    return _article_evidence_text(article)


def _spacex_event_claim(article):
    return bool(SPACEX_EVENT_ENTITY.search(_spacex_event_text(article)))


def _spacex_event_anchors(article):
    text = _spacex_event_text(article)
    vehicles = {
        name for name, pattern in SPACEX_VEHICLE_PATTERNS.items()
        if pattern.search(text)
    }
    actions = {
        name for name, pattern in SPACEX_EVENT_ACTION_PATTERNS.items()
        if pattern.search(text)
    }
    missions = {
        re.sub(r"[\s-]+", "-", match.group(0).casefold())
        for match in SPACEX_MISSION_ID.finditer(text)
    }
    return vehicles, missions, actions


def _spacex_allowed_unscored_placeholder(article):
    """Recognize only the scoring pipeline's exact deferred-score contract."""
    score = article.get("score_data")
    resolution = str(article.get("_score_resolution") or "")
    allowed_reasons = SPACEX_ALLOWED_DEFERRED_REASON_BY_RESOLUTION.get(
        resolution
    )
    zero_fields = (
        "innovation_score",
        "traffic_score",
        "tech_score",
        "commercial_score",
        "hype_score",
        "macro_score",
    )
    return (
        isinstance(score, dict)
        and allowed_reasons is not None
        and score.get("_unscored_placeholder_contract")
        == SPACEX_UNSCORED_PLACEHOLDER_CONTRACT
        and score.get("_unscored_reason_code") in allowed_reasons
        and score.get("is_relevant") is False
        and score.get("is_vague_or_roundup") is True
        and score.get("event_type") == "unscored"
        and score.get("industrial_claims") == []
        and score.get("market_only_claims") == []
        and all(score.get(field) == 0 for field in zero_fields)
        and bool(str(score.get("prompt_version") or "").strip())
        and bool(str(score.get("reasoning_chain") or "").strip())
        and score.get("reasoning_chain") == score.get("justification")
        and article.get("_score_cache_hit") is False
    )


def _spacex_event_context_matches(article, primary, *, max_days):
    """Validate provenance, issuer, event type and publication window."""
    if not _strict_spacex_primary(primary) or not _spacex_event_claim(article):
        return False
    primary_score = primary.get("score_data")
    deferred_placeholder = _spacex_allowed_unscored_placeholder(primary)
    if not (
        isinstance(primary_score, dict)
        and (
            primary_score.get("is_relevant") is True
            or deferred_placeholder
        )
    ):
        return False
    published = _published_datetime(article)
    primary_published = _published_datetime(primary)
    event_type = str((article.get("score_data") or {}).get("event_type") or "")
    primary_event_type = (
        ""
        if deferred_placeholder
        else str((primary.get("score_data") or {}).get("event_type") or "")
    )
    if (
        published is None
        or primary_published is None
        or abs((published - primary_published).total_seconds()) > max_days * 86400
        or not event_type
        or (primary_event_type and not _compatible_event(primary, event_type))
        or (
            not primary_event_type
            and event_type not in set(primary.get("authority_for") or [])
        )
    ):
        return False
    return True


def _spacex_primary_verified_relevant(primary, article_event_type):
    """Use audited source authority when the official item was not AI-scored."""
    score = primary.get("score_data")
    if _spacex_allowed_unscored_placeholder(primary):
        return (
            _strict_spacex_primary(primary)
            and str(article_event_type or "")
            in set(primary.get("authority_for") or [])
        )
    if isinstance(score, dict) and "is_relevant" in score:
        return score.get("is_relevant") is True
    if isinstance(score, dict) and score.get("event_type"):
        return False
    return False


def _same_spacex_event(article, primary, *, max_days):
    """Infer one SpaceX event only from an exact shared mission identifier.

    Vehicle and action overlap alone is not unique: SpaceX can launch multiple
    Falcon 9 payloads within the same three-day window. Non-explicit matching
    therefore requires the same recognized mission/flight identifier plus the
    same action. Exact URL citations select the provenance method but must pass
    these same event anchors; a background/related link grants no exception.
    """
    if not _spacex_event_context_matches(
        article, primary, max_days=max_days
    ):
        return False
    vehicles, missions, actions = _spacex_event_anchors(article)
    primary_vehicles, primary_missions, primary_actions = (
        _spacex_event_anchors(primary)
    )
    return (
        bool(missions & primary_missions)
        and bool(actions & primary_actions)
        and (
            not vehicles
            or not primary_vehicles
            or bool(vehicles & primary_vehicles)
        )
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
        "version": cluster["event_cluster_version"],
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


def _regulator_issuer_corroboration(article, regulator, cluster):
    payload = _corroboration(
        regulator,
        REGULATOR_ISSUER_CORROBORATION_METHOD,
    )
    payload.update(_event_cluster_payload(article, regulator, cluster))
    payload.update(
        {
            "issuer_identity": str(article["issuer_identity"]),
            "publisher_identity": str(article["publisher_identity"]),
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
        and (
            (article.get("score_data") or {}).get("event_type")
            not in {None, "non_industrial"}
            or _strict_spacex_primary(article)
        )
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
        article.pop("event_cluster", None)
    for article in articles:
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
            regulator_matches = []
            for primary in primaries:
                primary_published = _published_datetime(primary)
                cluster = _regulator_issuer_pair(article, primary)
                if (
                    primary_published is None
                    or abs((published - primary_published).total_seconds())
                    > max_days * 86400
                    or not cluster
                ):
                    continue
                regulator_matches.append((primary, cluster))
            if len(regulator_matches) == 1:
                primary, cluster = regulator_matches[0]
                corroboration = _regulator_issuer_corroboration(
                    article, primary, cluster
                )
                article["primary_corroboration"] = corroboration
                article["_primary_corroboration_verified_relevant"] = True
                primary["event_cluster"] = {
                    key: value
                    for key, value in corroboration.items()
                    if key.startswith("event_") or key in {"milestone"}
                }
                continue
            if regulator_matches:
                continue
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
        # Commercial-space discovery reports are especially prone to broad
        # entity overlap (many unrelated stories say only "SpaceX").  They
        # therefore use a closed event-level path: one real, same-batch
        # ``spacex_updates`` record must match vehicle/mission, event action,
        # event type and time. Registry hints, NASA/FAA records and generic
        # token overlap cannot authenticate the issuer's separate event.
        if article_tier not in {"T0", "T1"} and _spacex_event_claim(article):
            exact_matches = [
                primary
                for primary in primaries
                if id(primary) != id(article)
                and _same_spacex_event(
                    article, primary, max_days=max_days
                )
                and _references_exact_article(article, primary)
            ]
            if len(exact_matches) == 1:
                primary = exact_matches[0]
                method = EXPLICIT_PRIMARY_URL_METHOD
            else:
                if exact_matches:
                    continue
                inferred_matches = [
                    primary
                    for primary in primaries
                    if id(primary) != id(article)
                    and _same_spacex_event(
                        article, primary, max_days=max_days
                    )
                ]
                if len(inferred_matches) != 1:
                    continue
                primary = inferred_matches[0]
                method = SAME_BATCH_CORROBORATION_METHOD
            article["primary_corroboration"] = _corroboration(primary, method)
            article["_primary_corroboration_verified_relevant"] = (
                _spacex_primary_verified_relevant(primary, event_type)
            )
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

        if article_tier == "T1":
            # Conditional first-party T1 claims have a closed authorization
            # set. Registry URL hints remain visible for audit above, but a T1
            # claim must never fall through to fuzzy token overlap intended
            # for discovery media.
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
