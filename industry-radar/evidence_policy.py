"""Deterministic evidence and industrial-milestone policy."""

import re


EVIDENCE_POLICY_VERSION = "industrial-evidence-v3-research-lanes"


PRIMARY_EVIDENCE_STATES = frozenset(
    {"authoritative_record", "primary_claim", "primary_supported"}
)
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
            r"regulator approved|regulatory approval|authorized for commercial use|"
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
    if tier == "T0":
        evidence_state = "authoritative_record"
    elif tier == "T1":
        evidence_state = "primary_claim"
    elif has_verified_primary:
        evidence_state = "primary_supported"
    else:
        evidence_state = "discovery_only"
    article["evidence_state"] = evidence_state

    configured_trade = article.get("trade_eligible", False)
    authority_for = set(article.get("authority_for") or [])
    article["trade_evidence_eligible"] = bool(
        tier == "T0"
        and configured_trade is True
        and (not authority_for or event_type in authority_for)
    )
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
