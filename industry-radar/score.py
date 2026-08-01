import os
import json
import logging
import math
import re
from dotenv import load_dotenv
from llm_router import _call_llm_with_fallback
from run_date import logical_date_text
from provider_errors import log_provider_error

load_dotenv()
logger = logging.getLogger(__name__)

SCORING_PROMPT_VERSION = "dual-track-v3-industry-events"

INDUSTRIAL_EVENT_TYPES = {
    "technical_breakthrough",
    "product_launch",
    "capacity_capex",
    "supply_chain",
    "industrial_policy",
    "funding_with_use",
    "commercial_deployment",
    "mixed_industrial_market",
    "other_industrial",
}
NON_INDUSTRIAL_EVENT_TYPES = {"market_only", "non_industrial"}
EVENT_TYPES = INDUSTRIAL_EVENT_TYPES | NON_INDUSTRIAL_EVENT_TYPES

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
    )
)
_LOCAL_REJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bmorning brief\b|\bweekly digest\b|\bnews roundup\b",
        r"早报|晚报|晨报|新闻汇总|氪星晚报|点1氪",
        r"\bblack friday\b|\bprime day\b|\bsave \$?\d+",
        r"购物指南|优惠精选|限时优惠",
    )
)


class ScoreValidationError(ValueError):
    """Raised when scoring configuration or LLM output violates the contract."""


def _article_policy_text(article):
    return " ".join(
        str(article.get(field) or "")
        for field in ("title", "summary", "content")
    )[:4000]


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
    if has_market_signal and not has_industrial_action:
        return "market_only"
    if has_industrial_action:
        return "industry_candidate"
    return "llm"


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
    if rejection:
        pass
    elif explicit_non_industrial:
        rejection = event_type
    elif event_type in INDUSTRIAL_EVENT_TYPES and not explicit_industrial:
        rejection = "missing_industrial_claim"
    elif not explicit_industrial and local_route in {"market_only", "reject"}:
        rejection = local_route

    if rejection:
        gated["is_relevant"] = False
        for field in ("tech_score", "commercial_score", "hype_score", "macro_score"):
            if isinstance(gated.get(field), (int, float)):
                gated[field] = min(gated[field], 40)
        for field in ("innovation_score", "traffic_score"):
            if isinstance(gated.get(field), (int, float)):
                gated[field] = min(float(gated[field]), 4.0)
        gated["industry_policy_rejection"] = rejection
        gated["justification"] = (
            "REJECTED: no independently useful industrial event remains after "
            "removing market-price, trading-volume, and valuation claims."
        )
    return gated


def _current_date_text():
    return logical_date_text()


def _strict_rubric(config, current_date):
    language = config.get("output", {}).get("language", "Chinese")
    return f"""
    Current Date: {current_date}

    CRITICAL REJECTION RULES: is_relevant MUST be false for roundups/digests,
    shopping deals or advertisements, re-hashed old news, pure stock-price moves,
    pure theoretical research without a near-term industry application, and vague
    macro commentary without concrete technical or quantitative evidence.

    CRITICAL SCORING ANCHORS (all four sub-scores are integer 0-100):
    - 90-100: Global paradigm shift or independently verified breakthrough.
    - 70-89: Major industry milestone, >$100M financing, critical giant product
      launch, or major structural policy change.
    - 40-69: Routine product update, $10M-$50M financing, steady earnings, or
      incremental technical improvement.
    - 0-39: Gossip, minor updates, generic PR, or no measurable real-world impact.

    Before scoring, explicitly evaluate barrier_to_entry, market_size, and
    immediacy. Set is_vague_or_roundup=true when evidence is insufficient and in
    that case set is_relevant=false. Provide reasoning_chain before scores,
    a one-sentence justification in {language}, translated_title, and a
    one-sentence translated_summary no longer than 50 characters.
    """


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
    current_date = _current_date_text()
    rubric = _strict_rubric(config, current_date)
    prompt = f"""
    You are an expert industry analyst and VC. Evaluate this tech news article based on the dual-track criteria.
    
    Target Industries: {', '.join([ind['name'] for ind in config.get('industries', [])])}
    
    Criteria:
    {config.get('importance_criteria', '')}
    
    Article Title: {article['title']}
    Article Summary: {article['summary']}
    Published At: {article.get('published_at', 'unknown')}
    Source: {article.get('source', 'unknown')}

    {rubric}
    
    Tasks:
    1. Classify the article's primary event as exactly one of:
       technical_breakthrough, product_launch, capacity_capex, supply_chain,
       industrial_policy, funding_with_use, commercial_deployment,
       mixed_industrial_market, other_industrial, market_only, non_industrial.
       Extract `industrial_claims` that remain useful after removing all stock
       price, market-cap, trading-volume, valuation, analyst-rating and fund-flow
       claims. Extract those removed claims separately as `market_only_claims`.
       An IPO/funding story is industrial only when it states a concrete use of
       proceeds, capacity project, R&D program, product/customer validation, or
       supply-chain effect. A technology-company name alone is not an industrial
       event.
    2. Determine if this article is relevant to the tech industry (True/False).
       **CRITICAL REJECTION RULES: You MUST set is_relevant to False if the article is:**
       - A news roundup, summary, or digest (e.g., "Top 10 news", "Morning brief", "Weekly digest", "8点1氪", "氪星晚报", "晚报").
       - A shopping deal, discount, or advertisement (e.g., "Prime Day deals", "Black Friday", "Save $50 on...", "优惠精选", "购物指南").
       - Re-hashed old news or an old event being re-reported as new (e.g., "炒冷饭" - a breakthrough or event that actually happened months or years prior to the Current Date). If you recognize the event as historical relative to the Current Date, YOU MUST REJECT IT by setting is_relevant to False.
       - Vague, generic, or purely macro-level commentary lacking specific technical details, quantitative data, or concrete innovations (e.g., "market is growing", "competition intensifies", or "many companies are releasing models" without specifying unique technical specs).
       - Pure stock-price, trading-volume, market-cap, index, analyst-rating, or
         fund-flow news without a separate concrete industrial claim.
    3. Explicitly determine if this article is a vague roundup, digest, or macro-level commentary without concrete technical data. Set `is_vague_or_roundup` to True if it is.
    4. Output 4 core sub-scores (0-100 integers): 'tech_score', 'commercial_score', 'hype_score', 'macro_score'.
       **CRITICAL SCORING ANCHORS - YOU MUST STRICTLY FOLLOW THESE RUBRICS:**
       - **90-100**: Global paradigm shift or absolute breakthrough (e.g., AGI achieved, TSMC 1nm success, cure for cancer). Will fundamentally change the world immediately.
       - **70-89**: Major industry milestone, highly impactful VC funding (> $100M), critical product launch by a tech giant (e.g., Apple Vision Pro, GPT-4), or a massive structural policy change.
       - **40-69**: Routine product updates, moderate funding rounds ($10M-$50M), steady earnings reports, or incremental technical improvements.
       - **0-39**: Trivial gossip, extremely minor updates, generic executive PR talk, or things with no real-world impact.
    5. Provide structured reasoning before scoring. You must explicitly evaluate:
       - 'barrier_to_entry': How hard is this to replicate?
       - 'market_size': Is the target market massive or niche?
       - 'immediacy': Is the impact happening right now, or years in the future?
    6. Output a 'reasoning_chain' summarizing the structured reasoning.
    7. Provide a concise 1-sentence justification explaining the scores in {config.get('output', {}).get('language', 'Chinese')}.
    8. Provide the translation of the 'Article Title' into {config.get('output', {}).get('language', 'Chinese')}. For a mixed article, title the industrial event rather than its stock-price reaction.
    9. Provide a HIGHLY CONDENSED summary of the article content. **CRITICAL RULE: The translated_summary MUST be ONE SINGLE SENTENCE and MUST NOT exceed 50 Chinese characters. Be extremely brief.**
    
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
    
    result = _call_llm_with_fallback(prompt, config, title_context=article['title'][:30])
    result = _validate_score_result(result)
    llm_meta = result.pop("_llm", {})
    if isinstance(llm_meta, dict) and llm_meta.get("provider") and llm_meta.get("model"):
        result["llm_provider"] = llm_meta["provider"]
        result["llm_model"] = llm_meta["model"]
        result["llm_degraded"] = bool(llm_meta.get("degraded", False))
    result["source_confidence"] = (
        "trusted" if article.get("source") in config.get("trusted_sources", []) else "standard"
    )
    result["prompt_version"] = SCORING_PROMPT_VERSION
    _apply_composite_scores(result, weights)
    return apply_industry_relevance_gate(article, result)

def deduplicate_articles(articles, config):
    if len(articles) <= 1:
        return articles
        
    # Sort articles by published_at (earliest first)
    sorted_articles = sorted(articles, key=lambda x: x.get('published_at', '9999-12-31'))
    import difflib
    
    # Pre-deduplicate using local string matching to save LLM tokens
    local_dedup_groups = [] # list of lists of articles
    for a in sorted_articles:
        long_text = a.get('content', a.get('summary', ''))
        if long_text: long_text = long_text[:800]
        text_to_match = (a.get('title', '') + " " + long_text).lower()
        
        found_group = False
        for group in local_dedup_groups:
            # Compare against the first article in the group
            rep = group[0]
            rep_text = rep.get('content', rep.get('summary', ''))
            if rep_text: rep_text = rep_text[:800]
            rep_match = (rep.get('title', '') + " " + rep_text).lower()
            
            similarity = difflib.SequenceMatcher(None, text_to_match, rep_match).ratio()
            if similarity > 0.85:
                group.append(a)
                found_group = True
                break
                
        if not found_group:
            local_dedup_groups.append([a])
            
    print(f"Local pre-deduplication grouped {len(sorted_articles)} articles into {len(local_dedup_groups)} groups.", flush=True)

    if len(local_dedup_groups) <= 1:
        # If local grouping already reduced it to 1, just return the first of the group
        final_list = []
        for g in local_dedup_groups:
            best_article = max(g, key=lambda x: x.get('score_data', {}).get('innovation_score', 0) + x.get('score_data', {}).get('traffic_score', 0))
            final_list.append(best_article)
        return final_list

    # Prepare payload for LLM from the reduced groups
    payload = []
    for i, group in enumerate(local_dedup_groups):
        a = group[0] # Use the representative for LLM scoring
        long_text = a.get('content', a.get('summary', ''))
        if long_text:
            long_text = long_text[:250]
        
        payload.append({
            "id": i,
            "title": a.get('title', ''),
            "text": long_text
        })
        
    prompt = f"""
    You are a professional industry analyst. I have a list of tech news articles. Some of them are reporting on the exact same underlying event, just from different news outlets (e.g., they might use slightly different numbers or phrasing to describe the same event).
    Your task is to identify all duplicates and group them together.
    
    CRITICAL GROUPING RULES:
    1. If two articles are about the EXACT SAME company's funding round, valuation, or acquisition, THEY ARE DUPLICATES. Even if one highlights "$5B valuation" and the other highlights "$800M funding" or "$1B sales", if it's the same company's milestone event, GROUP THEM.
    2. If two articles are about the same product launch or major update from the same company, GROUP THEM.
    3. Be aggressive in grouping. We want to avoid reading about the same company's event twice.

    Here is the JSON list of articles:
    {json.dumps(payload, ensure_ascii=False, indent=2)}

    Return your answer strictly in JSON format matching this schema:
    {{
      "groups": [[int, ...], [int]] // A list of lists of IDs. Each inner list represents a unique event and contains the IDs of articles discussing it.
    }}
    """
    
    try:
        res = _call_llm_with_fallback(prompt, config, system_prompt="You are a helpful assistant designed to output JSON.", title_context="dedup_batch")
        groups = res.get("groups", [])
    except Exception as e:
        log_provider_error(
            logger,
            e,
            provider="configured_llm_chain",
            operation="deduplicate_articles",
            retryable=False,
            degraded_allowed=True,
        )
        print(f"LLM Deduplication error: {e}. Falling back to returning original articles.", flush=True)
        # Fallback: just pick the best from each local group
        final_list = []
        for g in local_dedup_groups:
            best_article = max(g, key=lambda x: x.get('score_data', {}).get('innovation_score', 0) + x.get('score_data', {}).get('traffic_score', 0))
            final_list.append(best_article)
        return final_list
        
    final_articles = []
    processed_group_ids = set()
    for group in groups:
        if not isinstance(group, list) or not group:
            continue
        valid_group_indices = []
        for idx in group:
            if (
                type(idx) is int
                and 0 <= idx < len(local_dedup_groups)
                and idx not in processed_group_ids
                and idx not in valid_group_indices
            ):
                valid_group_indices.append(idx)
        if not valid_group_indices:
            continue
            
        # Flatten the local groups corresponding to the LLM chosen indices into a single big group
        combined_articles = []
        for idx in valid_group_indices:
            combined_articles.extend(local_dedup_groups[idx])
            processed_group_ids.add(idx)
            
        base_article = combined_articles[0]
        
        if len(combined_articles) > 1:
            sources = set([base_article.get('source', '')])
            max_inn = base_article.get('score_data', {}).get('innovation_score', 0)
            max_tra = base_article.get('score_data', {}).get('traffic_score', 0)
            
            # Collect all unique titles, summaries, and justifications
            titles_to_merge = []
            summaries_to_merge = []
            justs_to_merge = []
            
            # Add base article
            ds_base = base_article.get('score_data', {})
            if base_article.get('title'): titles_to_merge.append(base_article['title'])
            if ds_base.get('translated_title'): titles_to_merge.append(ds_base['translated_title'])
            if base_article.get('summary'): summaries_to_merge.append(base_article['summary'])
            if ds_base.get('translated_summary'): summaries_to_merge.append(ds_base['translated_summary'])
            if ds_base.get('justification'): justs_to_merge.append(ds_base['justification'])
            
            for dup_art in combined_articles[1:]:
                sources.add(dup_art.get('source', ''))
                ds = dup_art.get('score_data', {})
                max_inn = max(max_inn, ds.get('innovation_score', 0))
                max_tra = max(max_tra, ds.get('traffic_score', 0))
                
                if dup_art.get('title') and dup_art['title'] not in titles_to_merge: titles_to_merge.append(dup_art['title'])
                if ds.get('translated_title') and ds['translated_title'] not in titles_to_merge: titles_to_merge.append(ds['translated_title'])
                if dup_art.get('summary') and dup_art['summary'] not in summaries_to_merge: summaries_to_merge.append(dup_art['summary'])
                if ds.get('translated_summary') and ds['translated_summary'] not in summaries_to_merge: summaries_to_merge.append(ds['translated_summary'])
                just = ds.get('justification', '')
                if just and just not in justs_to_merge: justs_to_merge.append(just)
                    
            if len(sources) > 1:
                max_tra = min(10.0, max_tra + (len(sources) - 1) * 0.5)
                
            if 'score_data' not in base_article:
                base_article['score_data'] = {}
                
            # Call LLM to synthesize
            lang = config.get('output', {}).get('language', 'Chinese')
            synth_prompt = f"""
            You are a master news editor. I have multiple news articles reporting on the exact same event from different angles or highlighting different metrics.
            Your task is to synthesize them into ONE perfect, comprehensive summary.
            
            Collected Titles:
            {json.dumps(titles_to_merge, ensure_ascii=False)}
            
            Collected Summaries:
            {json.dumps(summaries_to_merge, ensure_ascii=False)}
            
            Collected Editor Justifications:
            {json.dumps(justs_to_merge, ensure_ascii=False)}
            
            Please generate:
            1. A 'translated_title' in {lang} that captures all key metrics (e.g. if one says 800M funding and another says 5B valuation, include both if possible, or pick the most impactful).
            2. A 'translated_summary' in {lang} that is ONE SINGLE SENTENCE (MAX 50 CHARS) synthesizing the most important facts.
            3. A 'justification' in {lang} (1 sentence) combining the viewpoints of why this event is highly important.
            
            Return STRICTLY in JSON matching this schema:
            {{
              "translated_title": "string",
              "translated_summary": "string",
              "justification": "string"
            }}
            """
                
            try:
                synth_res = _call_llm_with_fallback(synth_prompt, config, system_prompt="You are a helpful JSON-outputting news editor.", title_context="News Synthesis")
                if synth_res:
                    if synth_res.get("translated_title"):
                        base_article['score_data']['translated_title'] = synth_res["translated_title"]
                    if synth_res.get("translated_summary"):
                        base_article['score_data']['translated_summary'] = synth_res["translated_summary"]
                    if synth_res.get("justification"):
                        base_article['score_data']['justification'] = synth_res["justification"]
                else:
                    base_article['score_data']['justification'] = " | ".join(justs_to_merge)
            except Exception as e:
                log_provider_error(
                    logger,
                    e,
                    provider="configured_llm_chain",
                    operation="synthesize_duplicate_event",
                    retryable=False,
                    degraded_allowed=True,
                )
                print(f"Synthesis failed: {e}")
                base_article['score_data']['justification'] = " | ".join(justs_to_merge)
                
            base_article['source'] = ", ".join([s for s in sources if s])
            base_article['score_data']['innovation_score'] = round(float(max_inn), 1)
            base_article['score_data']['traffic_score'] = round(float(max_tra), 1)
                
            final_articles.append(base_article)
        else:
            final_articles.append(base_article)
        
    # Add each omitted local group exactly once. `processed_group_ids` contains
    # local-group indices, never indices from the original article list.
    for group_id, local_group in enumerate(local_dedup_groups):
        if group_id not in processed_group_ids:
            best_article = max(
                local_group,
                key=lambda x: x.get('score_data', {}).get('innovation_score', 0)
                + x.get('score_data', {}).get('traffic_score', 0),
            )
            final_articles.append(best_article)
            
    return final_articles

def pre_filter_articles_batch(articles_batch, config):
    current_date = _current_date_text()
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
    Current Date: {current_date}
    
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
    current_date = _current_date_text()
    rubric = _strict_rubric(config, current_date)
    payload = []
    for a in articles_batch:
        payload.append({
            "id": a["id"],
            "title": a["title"],
            "summary": a["summary"][:300],
            "published_at": a.get("published_at", "unknown"),
            "source": a.get("source", "unknown"),
        })
        
    prompt = f"""
    You are an expert industry analyst and VC. Evaluate these tech news articles based on dual-track criteria.
    
    Target Industries: {', '.join([ind['name'] for ind in config.get('industries', [])])}
    
    Criteria:
    {config.get('importance_criteria', '')}

    Published At is supplied per article.
    {rubric}
    
    Input Articles JSON:
    {json.dumps(payload, ensure_ascii=False)}
    
    For EACH article in the input, provide:
    1. Classify `event_type` as exactly one of: technical_breakthrough,
       product_launch, capacity_capex, supply_chain, industrial_policy,
       funding_with_use, commercial_deployment, mixed_industrial_market,
       other_industrial, market_only, non_industrial.
    2. Extract `industrial_claims` that remain useful after removing stock-price,
       trading-volume, market-cap, valuation, analyst-rating and fund-flow claims;
       put removed claims in `market_only_claims`.
    3. 'is_relevant' and 'is_vague_or_roundup' booleans. `market_only` and
       `non_industrial` MUST set is_relevant=false. A technology-company name
       alone is not an industrial event.
    4. 'barrier_to_entry', 'market_size', and 'immediacy' structured assessments.
    5. 'reasoning_chain': A short paragraph explaining the logic BEFORE scoring.
    6. 4 sub-scores (0-100 integers): 'tech_score', 'commercial_score', 'hype_score', 'macro_score'.
    7. 'justification': 1-sentence explanation of scores in {config.get('output', {}).get('language', 'Chinese')}
    8. 'translated_title': Translate title to {config.get('output', {}).get('language', 'Chinese')}. For mixed articles, title the industrial event, not the price reaction.
    9. 'translated_summary': HIGHLY CONDENSED summary (MAX 50 Chinese characters)
    
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

    trusted_sources = config.get("trusted_sources", [])
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
        res_item["source_confidence"] = (
            "trusted" if original_a.get("source") in trusted_sources else "standard"
        )
        res_item["prompt_version"] = SCORING_PROMPT_VERSION
        if isinstance(llm_meta, dict) and llm_meta.get("provider") and llm_meta.get("model"):
            res_item["llm_provider"] = llm_meta["provider"]
            res_item["llm_model"] = llm_meta["model"]
            res_item["llm_degraded"] = bool(llm_meta.get("degraded", False))
        validated_items.append(res_item)
    result["results"] = validated_items
                
    return result
