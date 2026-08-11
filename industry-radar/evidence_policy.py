"""Deterministic evidence and industrial-milestone policy."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit, urlunsplit


EVIDENCE_POLICY_VERSION = "industrial-evidence-v4-current-trade-gate"

SAME_BATCH_CORROBORATION_METHOD = "same_batch_event_match_v1"
EXPLICIT_PRIMARY_URL_METHOD = "same_batch_explicit_primary_url_v1"
REGISTERED_PRIMARY_URL_METHOD = "registered_explicit_primary_url_v1"
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
        if article.get("source_tier") in {"T0", "T1"}:
            continue
        score = article.get("score_data") or {}
        event_type = score.get("event_type")
        if not event_type or score.get("is_relevant") is not True:
            continue
        published = _published_datetime(article)
        if published is None:
            continue

        explicit_matches = []
        for reference_url in article.get("reference_urls") or []:
            for primary in primaries_by_url.get(
                _canonical_evidence_url(reference_url), ()
            ):
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
            continue
        if explicit_matches:
            continue

        registered_matches = []
        for reference in article.get("registered_primary_references") or []:
            authority_for = set(reference.get("authority_for") or [])
            if event_type not in authority_for and "other_industrial" not in authority_for:
                continue
            if reference.get("source_tier") not in {"T0", "T1"}:
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
            continue
        if registered_matches:
            continue

        tokens = _event_tokens(article)
        if len(tokens) < 3:
            continue

        matches = []
        for primary in primaries:
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


def classify_industrial_milestone(text, *, event_type=None):
    """Classify concrete cross-industry progress without relying on an LLM."""
    normalized = " ".join(str(text or "").split())
    lower = normalized.lower()
    future = bool(
        re.search(
            r"planned|plans? to|expected to|will |next year|future|roadmap|"
            r"计划|预计|有望|未来|明年",
            lower,
        )
    )
    patterns = (
        (
            "shipping",
            r"now shipping|shipping to customers|开始出货|已出货|交付客户",
        ),
        (
            "mass_production",
            r"entered (?:mass )?production|now in (?:mass )?production|"
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
            r"商业部署|商业化落地|投入运营",
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
    milestone = next(
        (name for name, pattern in patterns if re.search(pattern, lower)),
        "none",
    )
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
    production_state = (
        "future"
        if future
        else "current"
        if milestone in current_milestones
        else "not_production"
        if milestone != "none"
        else "none"
    )
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

    if tier != "T0":
        return {"eligible": False, "reason": "authoritative_source_required"}
    if configured_trade is not True:
        return {"eligible": False, "reason": "source_not_trade_enabled"}
    if authority_for and event_type not in authority_for:
        return {"eligible": False, "reason": "source_not_authoritative_for_event"}
    if production_state != "current":
        return {"eligible": False, "reason": "current_maturity_required"}
    if milestone not in TRADE_TRIGGER_MILESTONES:
        return {"eligible": False, "reason": "milestone_is_research_only"}
    return {
        "eligible": True,
        "reason": "current_authoritative_trade_milestone",
        "milestone": milestone,
        "production_state": production_state,
    }


def annotate_article_evidence(article):
    score = article.get("score_data") or {}
    industrial_claims = score.get("industrial_claims") or []
    text = " ".join(
        str(article.get(field) or "")
        for field in ("title", "summary", "content")
    )
    if isinstance(industrial_claims, list):
        text = " ".join([text, *(str(claim) for claim in industrial_claims)])
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
    corroboration = article.get("primary_corroboration") or {}
    has_same_batch_primary = (
        isinstance(corroboration, dict)
        and corroboration.get("method")
        in {
            SAME_BATCH_CORROBORATION_METHOD,
            EXPLICIT_PRIMARY_URL_METHOD,
            REGISTERED_PRIMARY_URL_METHOD,
        }
        and corroboration.get("primary_source_tier") in {"T0", "T1"}
        and bool(corroboration.get("primary_url"))
        and bool(corroboration.get("primary_title"))
        and bool(corroboration.get("primary_source_id"))
    )
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
