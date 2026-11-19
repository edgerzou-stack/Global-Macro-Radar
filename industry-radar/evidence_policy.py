"""Deterministic evidence and industrial-milestone policy."""

from evidence_milestones import (
    DISCOVERY_WATCH_MILESTONES,
    RESEARCH_WATCH_MILESTONES,
    TRADE_TRIGGER_MILESTONES,
    _article_evidence_text,
    _bind_comparison_status,
    _classify_article_industrial_milestone,
    _compact_identity,
    _contains_alias,
    _current_clinical_readout,
    _industrial_clauses,
    _normalized_aliases,
    _normalized_identity,
    classify_hardtech_milestone,
    classify_industrial_milestone,
)
import re

from evidence_corroboration import (
    EVIDENCE_POLICY_VERSION,
    EXPLICIT_PRIMARY_URL_METHOD,
    INDEPENDENT_T1_MUTUAL_CORROBORATION_METHOD,
    MILESTONE_CLASSIFIER_VERSION,
    OFFICIAL_T1_EVENT_CLUSTER_VERSION,
    PRIMARY_EVIDENCE_STATES,
    REGISTERED_PRIMARY_URL_METHOD,
    REGULATOR_ISSUER_CORROBORATION_METHOD,
    REGULATOR_ISSUER_EVENT_CLUSTER_VERSION,
    REGULATORY_ENTITY_ROLE_PATTERN,
    REGULATORY_SOURCE_IDS,
    SAME_BATCH_CORROBORATION_METHOD,
    SPACEX_ALLOWED_DEFERRED_REASON_BY_RESOLUTION,
    SPACEX_CANONICAL_HOST,
    SPACEX_CANONICAL_PATH,
    SPACEX_DOCUMENT_ID,
    SPACEX_EVENT_ACTION_PATTERNS,
    SPACEX_EVENT_ENTITY,
    SPACEX_MISSION_ID,
    SPACEX_SOURCE_ID,
    SPACEX_UNSCORED_PLACEHOLDER_CONTRACT,
    SPACEX_UPDATES_API_URL,
    SPACEX_VEHICLE_PATTERNS,
    _article_evidence_text,
    _article_host,
    _canonical_evidence_url,
    _cluster_evidence_sha256,
    _cluster_member_id,
    _compatible_event,
    _contains_alias,
    _corroboration,
    _current_trade_maturity,
    _directly_names_both_identities,
    _event_cluster_payload,
    _event_tokens,
    _independent_mutual_t1_pair,
    _mutual_t1_corroboration,
    _normalized_aliases,
    _normalized_identity,
    _published_datetime,
    _references_exact_article,
    _registered_first_party_host_matches,
    _registered_reference_corroboration,
    _regulator_issuer_corroboration,
    _regulator_issuer_pair,
    _same_spacex_event,
    _shared_unambiguous_product_name,
    _spacex_allowed_unscored_placeholder,
    _spacex_event_anchors,
    _spacex_event_claim,
    _spacex_event_context_matches,
    _spacex_event_text,
    _spacex_primary_verified_relevant,
    _strict_spacex_primary,
    _trade_maturity,
    _valid_primary_corroboration,
    attach_same_batch_primary_corroboration,
)


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
    if authority_for and event_type not in authority_for:
        return {"eligible": False, "reason": "source_not_authoritative_for_event"}
    if production_state != "current":
        return {"eligible": False, "reason": "current_maturity_required"}
    if milestone not in TRADE_TRIGGER_MILESTONES:
        return {"eligible": False, "reason": "milestone_is_research_only"}
    if conditional_t1 and not _valid_primary_corroboration(
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
        industrial = _classify_article_industrial_milestone(article)
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
    article["milestone_classifier_version"] = MILESTONE_CLASSIFIER_VERSION

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
