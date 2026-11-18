import hashlib
import os
import json
import logging
import math
import re
from llm_router import _call_llm_with_fallback
from llm_cost_policy import validate_unscored_priority_display
from provider_errors import log_provider_error
from event_contract import (
    EVENT_TYPES,
    INDUSTRIAL_EVENT_TYPES,
    NON_INDUSTRIAL_EVENT_TYPES,
)

logger = logging.getLogger(__name__)

SCORING_PROMPT_VERSION = "dual-track-v8-shared-industry-rubric"
SCORING_RULE_VERSION = "deterministic-industry-boundary-v6-semantic-market-review"
SCORING_RUBRIC_VERSION = "industry-evidence-v1"

_MARKET_ONLY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"股价",
        r"股票.{0,8}(?:上涨|下跌|大涨|暴涨|暴跌)",
        r"(?:首日|盘中|收盘).{0,8}(?:上涨|下跌|大涨|暴涨|收涨|收跌)",
        r"涨停|跌停|市值|成交额|换手率|大盘走势|券商评级|目标价",
        r"融资余额|资金流向|北向资金",
        r"\bstock price\b|\bmarket cap\b|\btrading volume\b|\btrading debut\b",
        r"\bshares?\b.{0,30}\b(?:jump|surge|rise|fall|drop|gain|lose)s?\b",
    )
)
_INDUSTRIAL_ACTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"技术突破|工艺升级|研发|量产|投产|扩产|产能|产线|新建.{0,8}(?:工厂|厂)",
        r"产品发布|推出.{0,20}(?:产品|平台|模型|芯片|设备)|交付|订单|客户验证",
        r"获(?:得)?批准|获(?:得)?认证|临床试验|供应链|产业政策|监管新规|国家标准",
        r"并购|收购|商业部署|商业化|开源",
        r"融资.{0,20}(?:用于|投向)|募集资金.{0,20}(?:用于|投向)|资本开支|设备采购",
        r"\btechnical breakthrough\b|\br&d\b|\bresearch and development\b",
        r"\bmass production\b|\bproduction line\b|\bcapacity expansion\b|\bcapex\b",
        r"\bfactory\b|\bproduct launch\b|\bcommercial deployment\b",
        r"\bsupply chain\b|\bregulation\b|\bindustrial policy\b",
        r"\bfunding\b.{0,30}\b(?:used|for|to build|to expand|to develop)\b",
        # A regulator or public research fund is an industry event only when
        # the same article supplies a concrete R&D, grant, approval, or
        # biotech/medical-development hook.  This keeps generic politics out
        # while retaining NIH/FDA news with a direct industry effect.
        r"\b(?:nih|fda|ema|nmpa)\b.{0,100}\b(?:grant|funding|research|approval|review|regulation|drug|biotech|clinical)\b",
        r"\b(?:grant|funding|research|approval|review|regulation|drug|biotech|clinical)\b.{0,100}\b(?:nih|fda|ema|nmpa)\b",
        r"\b(?:biotech|biomedical|medical research|drug development|clinical research)\b",
        r"(?:国家药监局|药监局|卫健委|科技部|国自然).{0,100}(?:研发|科研|拨款|基金|审批|监管|药物|生物技术|临床)",
    )
)
_LOCAL_REJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bmorning brief\b|\bweekly\s+(?:digest|roundup)\b|\bnews roundup\b",
        r"早报|晚报|晨报|新闻汇总|氪星晚报|点1氪",
        r"\bblack friday\b|\bprime day\b|\bsave \$?\d+",
        r"购物指南|优惠精选|限时优惠",
    )
)

# An absent industry keyword is not evidence that a story is price-only. This
# deliberately narrow vocabulary proves only a wholly market-text shortcut;
# company names, product names and any other prose require semantic review.
_PURE_MARKET_WORDS = frozenset({
    "a", "an", "and", "at", "by", "for", "from", "in", "is", "it",
    "after", "before", "open", "opening", "close", "closed",
    "of", "on", "the", "to", "with", "market", "move", "moves", "stock",
    "stocks", "share", "shares", "price", "prices", "cap", "capitalization",
    "trading", "volume", "turnover", "valuation", "valuations", "index",
    "indices", "rose", "rise", "rises", "rising", "surge", "surged",
    "surges", "fell", "fall", "falls", "dropped", "drop", "drops", "gained",
    "gain", "gains", "jump", "jumped", "jumps", "up", "down", "higher",
    "lower", "percent", "per", "cent", "today", "yesterday", "usd", "hkd",
    "cny", "dollar", "dollars", "million", "billion", "trillion",
    "increase", "increased", "decrease", "decreased",
})


class ScoreValidationError(ValueError):
    """Raised when scoring configuration or LLM output violates the contract."""


def _article_source_text(article):
    return " ".join(
        str(article.get(field) or "")
        for field in ("title", "summary", "content")
    )


def _article_policy_text(article):
    return _article_source_text(article)[:4000]


def _is_explicit_price_only_text(article):
    words = re.findall(r"[^\W\d_]+", _article_source_text(article).casefold())
    return bool(words) and all(word in _PURE_MARKET_WORDS for word in words)


def local_article_route(article):
    """Return a deterministic coarse route without replacing semantic scoring.

    The local layer only handles high-confidence boundaries. Concrete industrial
    actions bypass the lossy fast LLM pre-filter and proceed to detailed scoring.
    Clear market-only items fail closed. Ambiguous articles remain LLM-routed.
    """

    text = _article_policy_text(article)
    if any(pattern.search(text) for pattern in _LOCAL_REJECTION_PATTERNS):
        return "reject"
    has_market_signal = any(
        pattern.search(text) for pattern in _MARKET_ONLY_PATTERNS
    )
    has_industrial_action = any(
        pattern.search(text) for pattern in _INDUSTRIAL_ACTION_PATTERNS
    )
    if (has_market_signal and not has_industrial_action
            and _is_explicit_price_only_text(article)):
        return "market_only"
    if has_industrial_action:
        return "industry_candidate"
    return "llm"


def requires_unscored_priority(article):
    """Never claim a platform refusal from words found in an RSS article.

    Actual scoreless review is an explicit, audited review outcome.  A source
    headline containing 'president', 'bill', or 'death' is not such evidence.
    """

    return False


PRIORITY_INDUSTRY_REVIEW_FIELDS = frozenset(
    {"is_relevant", "event_type", "industrial_claims", "factual_basis", "reason"}
)


def validate_priority_industry_review(value, article):
    """Validate a human/editor factual decision without constructing a score."""

    if not isinstance(value, dict) or set(value) != PRIORITY_INDUSTRY_REVIEW_FIELDS:
        raise ValueError("priority industry review has invalid fields")
    if type(value["is_relevant"]) is not bool:
        raise ValueError("priority industry review is_relevant must be boolean")
    if value["event_type"] not in EVENT_TYPES:
        raise ValueError("priority industry review event_type is invalid")
    claims = value["industrial_claims"]
    if not isinstance(claims, list) or any(
        not isinstance(claim, str) or not claim.strip() for claim in claims
    ):
        raise ValueError("priority industry review industrial_claims are invalid")
    for field in ("factual_basis", "reason"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError(f"priority industry review {field} is empty")
    if value["factual_basis"].strip().casefold() not in _article_source_text(article).casefold():
        raise ValueError("priority industry review factual_basis is not in source")
    if value["is_relevant"]:
        if value["event_type"] not in INDUSTRIAL_EVENT_TYPES or not claims:
            raise ValueError("priority industry review lacks an industrial event")
    elif value["event_type"] not in NON_INDUSTRIAL_EVENT_TYPES or claims:
        raise ValueError("priority industry review exclusion is contradictory")
    return {
        **value,
        "industrial_claims": [claim.strip() for claim in claims],
        "factual_basis": value["factual_basis"].strip(),
        "reason": value["reason"].strip(),
    }


def unscored_priority_industry_eligibility(article):
    """Require a bound factual review; keyword routing is never admission."""

    review = article.get("priority_industry_review")
    if review is None:
        return False, "priority_industry_review_missing"
    try:
        reviewed = validate_priority_industry_review(review, article)
    except ValueError:
        return False, "priority_industry_review_invalid"
    if local_article_route(article) in {"reject", "market_only"}:
        return False, "priority_deterministic_rejection"
    return (
        (True, "reviewed_industry_event")
        if reviewed["is_relevant"]
        else (False, "reviewed_not_industry_relevant")
    )


def apply_industry_relevance_gate(article, score_data):
    """Enforce the industry-news boundary on fresh and cached score payloads."""

    gated = dict(score_data or {})
    event_type = gated.get("event_type")
    industrial_claims = gated.get("industrial_claims")
    explicit_industrial = (
        event_type in INDUSTRIAL_EVENT_TYPES
        and isinstance(industrial_claims, list)
        and any(str(claim).strip() for claim in industrial_claims)
    )
    explicit_non_industrial = event_type in NON_INDUSTRIAL_EVENT_TYPES
    local_route = local_article_route(article)

    rejection = gated.get("industry_policy_rejection")
    reviewed_justification = gated.get("justification")
    if rejection:
        pass
    elif local_route in {"market_only", "reject"}:
        # A cached model score cannot override a high-confidence deterministic
        # boundary such as a price-only story, deal page, or weekly digest.
        rejection = local_route
    elif explicit_non_industrial:
        rejection = event_type
    elif event_type in INDUSTRIAL_EVENT_TYPES and not explicit_industrial:
        rejection = "missing_industrial_claim"

    if rejection:
        # Old deterministic cache entries predate the strict score fixture
        # contract and may contain only the rejection metadata.  A rejected
        # row is safe to migrate only to one canonical zero-score shape: never
        # coerce arbitrary numeric values or preserve a composite score that
        # disagrees with its missing sub-scores.
        gated.update(
            {
                "is_relevant": False,
                "is_vague_or_roundup": (
                    True
                    if rejection == "reject"
                    else (
                        gated["is_vague_or_roundup"]
                        if type(gated.get("is_vague_or_roundup")) is bool
                        else True
                    )
                ),
                "event_type": (
                    gated["event_type"]
                    if gated.get("event_type") in NON_INDUSTRIAL_EVENT_TYPES
                    else "non_industrial"
                ),
                "industrial_claims": [],
                "market_only_claims": [
                    claim.strip()
                    for claim in gated.get("market_only_claims", [])
                    if isinstance(claim, str) and claim.strip()
                ]
                if isinstance(gated.get("market_only_claims"), list)
                else [],
                "barrier_to_entry": "none",
                "market_size": "none",
                "immediacy": "none",
                "reasoning_chain": "Deterministic boundary rejection",
                "tech_score": 0,
                "commercial_score": 0,
                "hype_score": 0,
                "macro_score": 0,
                "innovation_score": 0.0,
                "traffic_score": 0.0,
                "translated_title": str(article.get("title") or ""),
                "translated_summary": "",
            }
        )
        gated["industry_policy_rejection"] = rejection
        if rejection == "reject":
            gated["justification"] = (
                "REJECTED: deterministic policy identified a roundup, digest, "
                "deal, advertisement, or other non-event content."
            )
        elif rejection == "market_only":
            gated["justification"] = (
                "REJECTED: no independently useful industrial event remains after "
                "removing market-price, trading-volume, and valuation claims."
            )
        elif rejection in {"non_industrial", "invalid_event_contract"}:
            gated["justification"] = (
                reviewed_justification.strip()
                if isinstance(reviewed_justification, str)
                and reviewed_justification.strip()
                else "REJECTED: review found no concrete industrial event."
            )
        else:
            gated["justification"] = (
                "REJECTED: an industrial event type was returned without a "
                "supported industrial claim."
            )
    return gated


def build_scoring_rubric(config):
    """One readable, config-bound policy for API prompts and folder reviews."""

    output = config.get("output", {})
    threshold = output.get("min_score_to_keep", 8)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or not 0 <= threshold <= 10
    ):
        raise ScoreValidationError("min_score_to_keep must be finite in [0, 10]")
    language = output.get("language", "Chinese")
    industries = config.get("industries", [])
    if not isinstance(industries, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not item["name"].strip()
        for item in industries
    ):
        raise ScoreValidationError("industries must have non-empty names")
    return {
        "version": SCORING_RUBRIC_VERSION,
        "target_industries": [item["name"].strip() for item in industries],
        "importance_criteria": str(config.get("importance_criteria", "")),
        "event_type_definitions": {
            "technical_breakthrough": "Verified advance in a useful technology with concrete technical evidence.",
            "product_launch": "New product, model, platform, or equipment release with a concrete industry fact.",
            "capacity_capex": "New factory, production line, capacity, equipment purchase, or capital project.",
            "supply_chain": "Concrete supplier, component, sourcing, or delivery change.",
            "industrial_policy": "Specific rule, grant, approval, or research policy with a described industry effect.",
            "funding_with_use": "Financing with a stated use in R&D, capacity, product, customers, or supply chain.",
            "commercial_deployment": "Customer validation, order, launch into use, or actual industrial deployment.",
            "mixed_industrial_market": "A concrete industrial event reported alongside market-price or valuation claims.",
            "other_industrial": "Another specific industrial action supported by supplied facts.",
            "market_only": "Price, market cap, valuation, rating, trading, or financing amount without an independent industrial fact.",
            "non_industrial": "No supported concrete industrial event in the supplied material.",
        },
        "scoring_anchors": {
            "90-100": "Global paradigm shift or independently verified breakthrough.",
            "70-89": "Major industry milestone, impactful funding with stated use, critical product launch, or structural policy change.",
            "40-69": "Routine product update, moderate funding with stated use, or incremental technical improvement.",
            "0-39": "Minor update, generic PR, unsupported hype, or no measurable industry impact.",
        },
        "scoring_weights": _validate_weights(config),
        "min_score_to_keep": threshold,
        "output_language": str(language),
        "translated_summary_max_characters": 50,
        "review_rules": [
            "Review every supplied article independently using its title, summary, available content, source and authority metadata; cite only facts in that article.",
            "Confirm an independent concrete industrial fact before applying any score example in importance_criteria. A famous scientist's personnel move or a technology-company name alone is insufficient; if the article documents specific R&D, product, capacity, customer, or supply-chain effects of the move, evaluate those supported facts before scoring.",
            "A report of the same new event from another source remains independently industry-relevant when its own facts support it; report display deduplication happens after scoring and must not zero this article or its evidence.",
            "Reject roundups, shopping deals, advertisements, stale events presented as new, generic opinion, vague macro commentary, and theoretical research without a near-term industrial application.",
            "After removing stock price, trading, market cap, valuation, analyst ratings and fund flows, require an independent concrete industrial claim. A technology company name or financing amount alone is insufficient; IPO or funding needs a supported use or industrial effect.",
            "A concrete NIH/FDA research grant, clinical approval, regulation, or other policy can be industrial when the article explains its effect; politics or a regulator's name alone is insufficient.",
            "Classify one event_type and extract industrial_claims and market_only_claims from supplied facts. If evidence is too vague or a roundup, set is_vague_or_roundup=true and is_relevant=false.",
            "Evaluate barrier_to_entry, market_size and immediacy before assigning four integer 0-100 sub-scores: tech_score, commercial_score, hype_score and macro_score. Explain the evidence in reasoning_chain.",
            "Compute innovation=(tech*innovation.tech+commercial*innovation.commercial)/10 and traffic=(hype*traffic.hype+macro*traffic.macro)/10. min_score_to_keep is the report display threshold on either composite, not an instruction to inflate a score; relevance, source evidence, dedup and capacity gates remain separate.",
            "Provide a one-sentence justification, translated_title and one-sentence translated_summary in output_language; summary is at most translated_summary_max_characters Chinese characters. Never invent facts beyond summary_only evidence or claim to have read a full article when only a summary was captured.",
        ],
        "priority_review_rule": (
            "Only an actual reviewing-platform restriction may produce an unscored_priority outcome. "
            "It receives no numerical score and must still undergo the same factual industrial relevance, "
            "deal/advertisement and evidence-quality review. Only relevant items may receive Chinese "
            "priority display, isolated from normal selection, score cache and trading evidence."
        ),
    }


def scoring_rubric_sha256(config):
    return scoring_rubric_payload_sha256(build_scoring_rubric(config))


def scoring_rubric_payload_sha256(rubric):
    """Digest a frozen rubric without loading configuration or credentials."""

    return hashlib.sha256(
        json.dumps(
            rubric, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def validate_scoring_rubric(rubric, rubric_sha256):
    """Fail closed on a missing, altered, or unsupported frozen review policy."""

    required = {
        "version", "target_industries", "importance_criteria",
        "event_type_definitions", "scoring_anchors", "scoring_weights",
        "min_score_to_keep", "output_language",
        "translated_summary_max_characters", "review_rules",
        "priority_review_rule",
    }
    if not isinstance(rubric, dict) or set(rubric) != required:
        raise ScoreValidationError("scoring rubric has invalid fields")
    if rubric["version"] != SCORING_RUBRIC_VERSION:
        raise ScoreValidationError("scoring rubric version is unsupported")
    industries = rubric["target_industries"]
    if not isinstance(industries, list) or any(
        not isinstance(name, str) or not name.strip() for name in industries
    ):
        raise ScoreValidationError("scoring rubric target_industries are invalid")
    if not isinstance(rubric["importance_criteria"], str):
        raise ScoreValidationError("scoring rubric importance_criteria is invalid")
    if not isinstance(rubric["output_language"], str) or not rubric["output_language"].strip():
        raise ScoreValidationError("scoring rubric output_language is invalid")
    if rubric["translated_summary_max_characters"] != 50:
        raise ScoreValidationError("scoring rubric summary limit is invalid")
    if not isinstance(rubric["review_rules"], list) or any(
        not isinstance(rule, str) or not rule.strip()
        for rule in rubric["review_rules"]
    ):
        raise ScoreValidationError("scoring rubric review_rules are invalid")
    if not isinstance(rubric["priority_review_rule"], str) or not rubric["priority_review_rule"].strip():
        raise ScoreValidationError("scoring rubric priority_review_rule is invalid")
    expected = build_scoring_rubric(
        {
            "industries": [{"name": name} for name in industries],
            "importance_criteria": rubric["importance_criteria"],
            "scoring_weights": rubric["scoring_weights"],
            "output": {
                "language": rubric["output_language"],
                "min_score_to_keep": rubric["min_score_to_keep"],
            },
        }
    )
    if rubric != expected:
        raise ScoreValidationError("scoring rubric differs from supported policy")
    if (
        not isinstance(rubric_sha256, str)
        or len(rubric_sha256) != 64
        or any(char not in "0123456789abcdef" for char in rubric_sha256)
        or rubric_sha256 != scoring_rubric_payload_sha256(rubric)
    ):
        raise ScoreValidationError("scoring rubric hash does not match payload")
    return rubric


_STRICT_RUBRIC_HEADER = (
    "CRITICAL SCORING ANCHORS AND INDUSTRIAL REVIEW RULES (canonical JSON):\n"
)

_SINGLE_ARTICLE_PROMPT_TEMPLATE = """
    You are an industry analyst. Apply the canonical policy below to this
    article independently, using only the supplied evidence.

    {rubric}

    Article Title: {title}
    Article Summary: {summary}
    Article Content: {content}
    Published At: {published_at}
    Source: {source}
    Source Evidence Metadata: tier={source_tier},
    lane={source_lane},
    domains={source_domains},
    authority_for={authority_for}

    Classify and score this article under the rubric, then return only JSON.

    You must output strictly in JSON format matching this schema:
    {{
      "is_relevant": boolean,
      "is_vague_or_roundup": boolean,
      "event_type": string,
      "industrial_claims": [string],
      "market_only_claims": [string],
      "barrier_to_entry": string,
      "market_size": string,
      "immediacy": string,
      "reasoning_chain": string,
      "tech_score": integer,
      "commercial_score": integer,
      "hype_score": integer,
      "macro_score": integer,
      "justification": string,
      "translated_title": string,
      "translated_summary": string
    }}
    """

_BATCH_ARTICLE_PROMPT_TEMPLATE = """
    You are an industry analyst. Apply the canonical policy below to EACH
    supplied article independently using only its supplied evidence.
    Published At and source metadata are supplied per article; content_excerpt is only a
    bounded excerpt, so never claim to have read a full article from it.

    {rubric}

    Input Articles JSON:
    {payload_json}

    Classify and score each article under the rubric. Return only JSON.

    Return STRICTLY a JSON object matching this schema exactly:
    {{
      "results": [
        {{
          "id": integer,
          "is_relevant": boolean,
          "is_vague_or_roundup": boolean,
          "event_type": string,
          "industrial_claims": [string],
          "market_only_claims": [string],
          "barrier_to_entry": string,
          "market_size": string,
          "immediacy": string,
          "reasoning_chain": string,
          "tech_score": integer (e.g. 83),
          "commercial_score": integer (e.g. 76),
          "hype_score": integer (e.g. 90),
          "macro_score": integer (e.g. 60),
          "justification": string,
          "translated_title": string,
          "translated_summary": string
        }}
      ]
    }}
    """


def _strict_rubric(config):
    return (
        _STRICT_RUBRIC_HEADER
        + json.dumps(
            build_scoring_rubric(config), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
    )


def _validate_weights(config):
    weights = config.get("scoring_weights", {})
    definitions = (
        ("innovation", {"tech": 0.6, "commercial": 0.4}),
        ("traffic", {"hype": 0.6, "macro": 0.4}),
    )
    validated = {}
    for name, defaults in definitions:
        values = weights.get(name, defaults)
        if not isinstance(values, dict) or set(values) != set(defaults):
            raise ScoreValidationError(f"{name} weights must contain {sorted(defaults)}")
        clean = {}
        for key in defaults:
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ScoreValidationError(f"{name}.{key} weight must be numeric")
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ScoreValidationError(f"{name}.{key} weight must be finite and non-negative")
            clean[key] = value
        if not math.isclose(sum(clean.values()), 1.0, rel_tol=0, abs_tol=1e-9):
            raise ScoreValidationError(f"{name} weights must sum to 1")
        validated[name] = clean
    return validated


def _validate_score_result(result, expected_id=None, require_id=False):
    if not isinstance(result, dict):
        raise ScoreValidationError("score result must be an object")
    if require_id:
        if "id" not in result:
            raise ScoreValidationError("score result missing id")
        if result["id"] != expected_id:
            raise ScoreValidationError(f"score result id {result['id']!r} does not match {expected_id!r}")

    for field in ("is_relevant", "is_vague_or_roundup"):
        if type(result.get(field)) is not bool:
            raise ScoreValidationError(f"{field} must be a boolean")

    for field in ("tech_score", "commercial_score", "hype_score", "macro_score"):
        value = result.get(field)
        if type(value) is not int or not 0 <= value <= 100:
            raise ScoreValidationError(f"{field} must be an integer in [0, 100]")

    for field in (
        "barrier_to_entry",
        "market_size",
        "immediacy",
        "reasoning_chain",
        "justification",
        "translated_title",
        "translated_summary",
    ):
        if not isinstance(result.get(field), str):
            raise ScoreValidationError(f"{field} must be a string")
    validated = dict(result)
    if validated["is_relevant"]:
        try:
            display = validate_unscored_priority_display(
                {
                    "translated_title": validated["translated_title"],
                    "translated_summary": validated["translated_summary"],
                }
            )
        except ValueError as error:
            raise ScoreValidationError(f"translated_display is invalid: {error}") from error
        validated.update(display)
    if len(validated["translated_summary"]) > 50:
        validated["translated_summary"] = validated["translated_summary"][:50]
    event_type = validated.get("event_type")
    if event_type not in EVENT_TYPES:
        raise ScoreValidationError(
            f"event_type must be one of {sorted(EVENT_TYPES)}"
        )
    for field in ("industrial_claims", "market_only_claims"):
        claims = validated.get(field)
        if not isinstance(claims, list) or any(
            not isinstance(claim, str) or not claim.strip() for claim in claims
        ):
            raise ScoreValidationError(f"{field} must be a list of non-empty strings")
    if event_type in INDUSTRIAL_EVENT_TYPES and not validated["industrial_claims"]:
        validated["event_type"] = "non_industrial"
        validated["is_relevant"] = False
        validated["industry_policy_rejection"] = "invalid_event_contract"
        validated["justification"] = (
            "REJECTED: industrial event type was returned without a concrete "
            "industrial claim."
        )
    elif (
        event_type in NON_INDUSTRIAL_EVENT_TYPES
        and validated["industrial_claims"]
    ):
        validated["industrial_claims"] = []
        validated["is_relevant"] = False
        validated["industry_policy_rejection"] = "invalid_event_contract"
        validated["justification"] = (
            "REJECTED: model returned contradictory industrial and "
            "non-industrial classifications."
        )
    if validated["is_vague_or_roundup"]:
        validated["is_relevant"] = False
        validated["justification"] = (
            "REJECTED: Vague macro-commentary, roundup, or lacks concrete data."
        )
    return validated


def _apply_composite_scores(result, weights):
    result["innovation_score"] = (
        result["tech_score"] * weights["innovation"]["tech"]
        + result["commercial_score"] * weights["innovation"]["commercial"]
    ) / 10.0
    result["traffic_score"] = (
        result["hype_score"] * weights["traffic"]["hype"]
        + result["macro_score"] * weights["traffic"]["macro"]
    ) / 10.0
    return result

def score_article(article, config):
    weights = _validate_weights(config)
    rubric = _strict_rubric(config)
    prompt = _SINGLE_ARTICLE_PROMPT_TEMPLATE.format(
        rubric=rubric,
        title=article["title"],
        summary=article["summary"],
        content=str(article.get("content") or "")[:3000],
        published_at=article.get("published_at", "unknown"),
        source=article.get("source", "unknown"),
        source_tier=article.get("source_tier", "unknown"),
        source_lane=article.get("source_lane", "unknown"),
        source_domains=article.get("source_domains", []),
        authority_for=article.get("authority_for", []),
    )
    
    result = _call_llm_with_fallback(prompt, config, title_context=article['title'][:30])
    result = _validate_score_result(result)
    llm_meta = result.pop("_llm", {})
    if isinstance(llm_meta, dict) and llm_meta.get("provider") and llm_meta.get("model"):
        result["llm_provider"] = llm_meta["provider"]
        result["llm_model"] = llm_meta["model"]
        result["llm_degraded"] = bool(llm_meta.get("degraded", False))
    result["source_confidence"] = _source_confidence(article, config)
    result["prompt_version"] = SCORING_PROMPT_VERSION
    _apply_composite_scores(result, weights)
    return apply_industry_relevance_gate(article, result)


def _source_confidence(article, config):
    tier = article.get("source_tier")
    if tier == "T0":
        return "authoritative"
    if tier == "T1":
        return "primary"
    if tier == "T2":
        return "secondary"
    if tier == "T3":
        return "research_only"
    return (
        "trusted"
        if article.get("source") in config.get("trusted_sources", [])
        else "standard"
    )

def pre_filter_articles_batch(articles_batch, config):
    payload = []
    for a in articles_batch:
        payload.append({
            "id": a["id"],
            "title": a["title"],
            "summary": a["summary"][:100],
            "published_at": a.get("published_at", "unknown"),
        })
        
    prompt = f"""
    You are a fast content filter for a tech/VC radar. 
    You will receive a list of articles. For each article, determine if it is relevant to Hardcore Tech, Investment, or cutting-edge innovation.
    
    Target Industries: {', '.join([ind['name'] for ind in config.get('industries', [])])}
    CRITICAL REJECTION RULES: Return is_relevant=false if the article is:
    1. A news roundup/digest (e.g. "Morning brief", "晚报").
    2. A shopping deal, discount, ad (e.g. "Black Friday", "Save $50", "促销").
    3. Re-hashed old news or gossip.
    4. Pure stock-price, trading-volume, market-cap, index, analyst-rating, or
       fund-flow news with no concrete technology, product, capacity, R&D,
       deployment, supply-chain, regulatory, or use-of-funds claim.

    A concrete industrial policy, product launch, production/capacity project,
    customer deployment, clinical milestone, or funding with a stated industrial
    use MUST survive this fast filter and proceed to detailed scoring.
    
    Input JSON:
    {json.dumps(payload, ensure_ascii=False)}
    
    Return STRICTLY a JSON object matching this schema exactly:
    {{
      "results": [
        {{"id": integer, "is_relevant": boolean}}
      ]
    }}
    """
    
    result = _call_llm_with_fallback(prompt, config, title_context=f"Pre-filter Batch ({len(articles_batch)} items)")
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise ScoreValidationError("pre-filter results must be a list")
    expected_ids = [article["id"] for article in articles_batch]
    if len(set(expected_ids)) != len(expected_ids):
        raise ScoreValidationError("input article ids must be unique")
    returned_ids = []
    for item in result["results"]:
        if not isinstance(item, dict):
            raise ScoreValidationError("pre-filter item must be an object")
        if type(item.get("is_relevant")) is not bool:
            raise ScoreValidationError("pre-filter is_relevant must be a boolean")
        returned_ids.append(item.get("id"))
    missing_ids = [item_id for item_id in expected_ids if item_id not in returned_ids]
    duplicate_ids = {item_id for item_id in returned_ids if returned_ids.count(item_id) > 1}
    unknown_ids = [item_id for item_id in returned_ids if item_id not in expected_ids]
    if missing_ids:
        raise ScoreValidationError(f"pre-filter missing ids: {missing_ids}")
    if duplicate_ids:
        raise ScoreValidationError(
            f"pre-filter duplicate ids: {sorted(duplicate_ids, key=str)}"
        )
    if unknown_ids:
        raise ScoreValidationError(f"pre-filter unknown ids: {unknown_ids}")
    return result

def score_articles_batch(articles_batch, config):
    weights = _validate_weights(config)
    rubric = _strict_rubric(config)
    payload = []
    for a in articles_batch:
        payload.append({
            "id": a["id"],
            "title": a["title"],
            "summary": a["summary"][:300],
            "content_excerpt": str(a.get("content") or "")[:1200],
            "published_at": a.get("published_at", "unknown"),
            "source": a.get("source", "unknown"),
            "source_tier": a.get("source_tier", "unknown"),
            "source_lane": a.get("source_lane", "unknown"),
            "source_domains": a.get("source_domains", []),
            "authority_for": a.get("authority_for", []),
        })
        
    prompt = _BATCH_ARTICLE_PROMPT_TEMPLATE.format(
        rubric=rubric,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    
    result = _call_llm_with_fallback(prompt, config, title_context=f"Score Batch ({len(articles_batch)} items)")
    
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise ScoreValidationError("results must be a list")

    result_items = result["results"]

    expected_ids = [a["id"] for a in articles_batch]
    if len(set(expected_ids)) != len(expected_ids):
        raise ScoreValidationError("input article ids must be unique")
    returned_ids = [item.get("id") for item in result_items if isinstance(item, dict)]
    duplicate_ids = {item_id for item_id in returned_ids if returned_ids.count(item_id) > 1}
    missing_ids = [item_id for item_id in expected_ids if item_id not in returned_ids]
    unknown_ids = [item_id for item_id in returned_ids if item_id not in expected_ids]
    if missing_ids:
        raise ScoreValidationError(f"missing ids: {missing_ids}")
    if duplicate_ids:
        raise ScoreValidationError(f"duplicate ids: {sorted(duplicate_ids, key=str)}")
    if unknown_ids:
        raise ScoreValidationError(f"unknown ids: {unknown_ids}")

    llm_meta = result.get("_llm", {})
    validated_items = []
    article_by_id = {a["id"]: a for a in articles_batch}
    for raw_item in result_items:
        if not isinstance(raw_item, dict):
            raise ScoreValidationError("score result must be an object")
        article_id = raw_item.get("id")
        res_item = _validate_score_result(raw_item, expected_id=article_id, require_id=True)
        _apply_composite_scores(res_item, weights)
        original_a = article_by_id[article_id]
        res_item = apply_industry_relevance_gate(original_a, res_item)
        res_item["source_confidence"] = _source_confidence(original_a, config)
        res_item["prompt_version"] = SCORING_PROMPT_VERSION
        if isinstance(llm_meta, dict) and llm_meta.get("provider") and llm_meta.get("model"):
            res_item["llm_provider"] = llm_meta["provider"]
            res_item["llm_model"] = llm_meta["model"]
            res_item["llm_degraded"] = bool(llm_meta.get("degraded", False))
        validated_items.append(res_item)
    result["results"] = validated_items
    return result


def translate_unscored_priority_batch(articles_batch, config):
    """Generate Chinese display copy without requesting a score or judgment."""

    payload = [
        {
            "id": article["id"],
            "title": article.get("title", ""),
            "summary": str(article.get("summary") or "")[:600],
            "content": str(article.get("content") or "")[:1200],
        }
        for article in articles_batch
    ]
    prompt = f"""
    You are a Chinese news editor. Translate and condense each supplied article.
    Do not classify, rank, evaluate, recommend, or assign numerical scores.
    Preserve only factual wording from the supplied material.

    Input JSON:
    {json.dumps(payload, ensure_ascii=False)}

    Return STRICTLY one JSON object:
    {{
      "results": [
        {{
          "id": integer,
          "translated_title": "non-empty Chinese title",
          "translated_summary": "one Chinese sentence, no more than 50 characters"
        }}
      ]
    }}
    """
    result = _call_llm_with_fallback(
        prompt,
        config,
        system_prompt=(
            "You translate restricted news for display only. Never provide "
            "scores, rankings, predictions, recommendations, or analysis."
        ),
        title_context=f"Priority Translation Batch ({len(articles_batch)} items)",
    )
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise ScoreValidationError("priority translation results must be a list")
    expected_ids = [article["id"] for article in articles_batch]
    returned_ids = []
    displays = []
    for item in result["results"]:
        if not isinstance(item, dict) or set(item) != {
            "id", "translated_title", "translated_summary"
        }:
            raise ScoreValidationError("priority translation item has invalid fields")
        item_id = item.get("id")
        returned_ids.append(item_id)
        try:
            display = validate_unscored_priority_display(
                {
                    "translated_title": item["translated_title"],
                    "translated_summary": item["translated_summary"],
                }
            )
        except ValueError as error:
            raise ScoreValidationError(
                f"priority translation display is invalid: {error}"
            ) from error
        displays.append({"id": item_id, "priority_display": display})
    missing_ids = [item_id for item_id in expected_ids if item_id not in returned_ids]
    duplicate_ids = {item_id for item_id in returned_ids if returned_ids.count(item_id) > 1}
    unknown_ids = [item_id for item_id in returned_ids if item_id not in expected_ids]
    if missing_ids:
        raise ScoreValidationError(f"priority translation missing ids: {missing_ids}")
    if duplicate_ids:
        raise ScoreValidationError(
            f"priority translation duplicate ids: {sorted(duplicate_ids, key=str)}"
        )
    if unknown_ids:
        raise ScoreValidationError(
            f"priority translation unknown ids: {unknown_ids}"
        )
    return {"results": displays}


_REFERENCE_RUBRIC_CONFIG = {
    "industries": [{"name": "__INDUSTRY__"}],
    "importance_criteria": "__CRITERIA__",
    "output": {"language": "Chinese", "min_score_to_keep": 7},
    "scoring_weights": {
        "innovation": {"tech": 0.6, "commercial": 0.4},
        "traffic": {"hype": 0.6, "macro": 0.4},
    },
}

# Compute this from semantic prompt/rubric contracts rather than Python function
# source so comments, formatting, and non-semantic refactors do not invalidate
# validated caches while prompt/schema/rubric edits still bust stale entries.
SCORING_PROMPT_SHA256 = hashlib.sha256(
    json.dumps(
        {
            "prompt_version": SCORING_PROMPT_VERSION,
            "rubric_version": SCORING_RUBRIC_VERSION,
            "reference_rubric": build_scoring_rubric(_REFERENCE_RUBRIC_CONFIG),
            "strict_rubric_header": _STRICT_RUBRIC_HEADER,
            "single_prompt_template": _SINGLE_ARTICLE_PROMPT_TEMPLATE,
            "batch_prompt_template": _BATCH_ARTICLE_PROMPT_TEMPLATE,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()
