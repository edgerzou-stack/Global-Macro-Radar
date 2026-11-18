"""Industrial milestone and text-evidence classifiers."""

import re

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


_INDUSTRIAL_CLAUSE_BOUNDARY = re.compile(r"(?<!\d)\.|\.(?!\d)|[!?;。！？；\n]+")
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
_CLINICAL_PHASE = r"\bphase\s+(?:[123]|III|II|I)[ab]?\b"
_CLINICAL_READOUT = (
    _CLINICAL_PHASE + r"[^.!?;。！？；\n]{0,180}\b(?:results?|data|readout)\b|"
    + _CLINICAL_PHASE + r"[^.!?;。！？；\n]{0,180}\b(?:met|achieved|reached|did not meet|failed to meet)\b"
    r"[^.!?;。！？；\n]{0,100}\b(?:endpoint|endpoints)\b|"
    r"\b(?:met|achieved|reached)\b[^.!?;。！？；\n]{0,100}\bendpoints?\b"
    r"[^.!?;。！？；\n]{0,180}" + _CLINICAL_PHASE + r"|"
    r"pivotal trial[^.!?;。！？；\n]{0,140}\b(?:results?|data|readout|endpoints?)\b|"
    r"临床.{0,8}(?:达到终点|结果|数据)"
)
_COMPLETED_CLINICAL_READOUT = re.compile(
    r"\b(?:met|achieved|reached|did not meet|failed to meet)\b"
    r"[^.!?;。！？；\n]{0,100}\bendpoints?\b|"
    r"\b(?:reports?|reported|announces?|announced|presents?|presented|publishes?|published)\b"
    r"[^.!?;。！？；\n]{0,180}\b(?:results?|data|readout)\b|"
    r"\b(?:positive|negative|top[- ]line)\b[^.!?;。！？；\n]{0,100}\bresults?\b|"
    r"\b(?:results?|data)\b[^.!?;。！？；\n]{0,100}\b(?:show|showed|demonstrate|demonstrated)\b|"
    r"临床.{0,8}达到终点|(?:公布|发布).{0,20}临床.{0,8}(?:结果|数据)|临床.{0,8}结果显示",
    re.IGNORECASE,
)
_UNAVAILABLE_CLINICAL_RESULTS = re.compile(
    r"\bno\s+(?:clinical\s+)?results?\b|\bresults?\s+(?:are\s+|is\s+)?not\s+available\b|"
    r"\b(?:not|never)\s+(?:(?:yet|actually|ever|formally|been|already|officially)\s+){0,3}"
    r"(?:announce[ds]?|report(?:ed|s)?|publish(?:ed|es)?|present(?:ed|s)?)\b|"
    r"\b(?:tomorrow|next (?:week|month|quarter|year))\b|"
    r"\bto\s+(?:report|announce|publish|present)\b|"
    r"尚无.{0,8}(?:结果|数据)|尚未.{0,8}(?:公布|发布).{0,8}(?:结果|数据)",
    re.IGNORECASE,
)
_COMPLETED_APPROVAL = re.compile(
    r"\b(?:fda|ema|regulator)\s+(?:has\s+)?approv(?:ed|es?)\b|"
    r"\b(?:granted|received|obtained|secured)\s+(?:the\s+)?"
    r"(?:(?:necessary|regulatory|accelerated|marketing)\s+)*approvals?\b|"
    r"\bapproval\s+(?:(?:was|has been)\s+)?granted\b|"
    r"authorized for commercial use|获批|批准上市|监管批准|获准商业使用",
    re.IGNORECASE,
)
_NEGATED_APPROVAL = re.compile(
    r"\b(?:not|never)\s+(?:(?:yet|actually|ever|formally|been|already|officially)\s+){0,3}"
    r"(?:granted|received|obtained|secured|approved|authorized)\b|"
    r"\bno\s+(?:regulatory\s+)?approval\b|"
    r"(?:未|没有|尚未|不)(?:被)?(?:获批|批准上市|获得.{0,8}批准)|"
    r"\b(?:denied|refused)\s+(?:regulatory\s+)?approval\b",
    re.IGNORECASE,
)
_ACQUISITION_EVENT = re.compile(
    r"\bacquir(?:e|es|ed|ing)\b|\bacquisition\b|"
    r"\bagreed\s+(?:to\s+)?(?:buy|purchase)\b|收购|并购", re.I,
)
_PENDING_ACQUISITION = re.compile(
    r"\bagreed\s+(?:agreed\s+)?to\s+(?:acquire|buy|purchase)\b|"
    r"\b(?:deal|transaction|acquisition)\b.{0,100}\b(?:expected|will|pending|subject to)\b|"
    r"\b(?:pending|subject to)\b.{0,80}\b(?:approval|closing|clearance)\b|"
    r"\b(?:not yet|not)\s+(?:completed|closed|finalized)\b|"
    r"同意.{0,20}收购|待.{0,10}(?:审批|交割)|尚未.{0,10}完成", re.I,
)
_COMPLETED_ACQUISITION = re.compile(
    r"\b(?:completed|closed|finalized)\s+(?:the\s+|its\s+|an?\s+)?acquisition\b|"
    r"\bacquisition\s+(?:has\s+)?(?:closed|completed)\b|完成.{0,10}(?:收购|并购)", re.I,
)
_MODEL_NAME = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]*[- ]\d+(?:\.\d+)+(?:[- ]+[A-Za-z][A-Za-z0-9]*)?\b"
)
_LAUNCHED_PRODUCT = re.compile(
    r"\b(?i:releases?|released|launch(?:es|ed)?|introduc(?:es|ed)|unveils?)\s+"
    r"([A-Z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*)\b"
)
_COMPARISON_TABLE = re.compile(
    r"\bFeature\s+(?P<names>.{1,600}?)\s+Developer\s*/\s*API\s+ID\s+"
    r"(?P<ids>.{1,800}?)\s+Status\s+(?P<states>.{1,250}?)\s+Input price\b",
    re.I | re.S,
)
_COMPARISON_ID = re.compile(
    r"[A-Za-z][A-Za-z0-9]*\s*/\s*([a-z][a-z0-9]*(?:[.-][a-z0-9]+)+)", re.I,
)
_COMPARISON_STATE = re.compile(r"Generally available|Not available|Preview|Beta", re.I)
_ANONYMOUS_DEPLOYMENT_LIMIT = re.compile(
    r"^(?:it|this (?:model|product|service)|the (?:model|product|service))\b", re.I,
)
_NEGATED_DEPLOYMENT = re.compile(
    r"\b(?:not|never|no longer|(?:is|are|was|were|has|have|wo|ca)n['’]t)\s+"
    r"(?:(?:yet|currently|generally|been|be|actually|broadly|fully|publicly|commercially)\s+){0,4}"
    r"(?:available|live|deployed)\b|尚未.{0,8}(?:可用|上线|部署)|不再.{0,8}(?:可用|上线)", re.I,
)


def _compact_identity(text):
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())


def _bind_comparison_status(text, title):
    """Interpret only a complete, uniquely attributed flattened API table.

    This never changes stored source material and cannot alone prove a current
    deployment. Ambiguous layouts remain untouched and therefore fail closed.
    """
    def bind(match):
        id_matches = list(_COMPARISON_ID.finditer(match["ids"]))
        ids = [item[1] for item in id_matches]
        states = _COMPARISON_STATE.findall(match["states"])
        if (
            not 2 <= len(ids) <= 8 or len(ids) != len(states)
            or len(set(ids)) != len(ids)
            or _COMPARISON_ID.sub("", match["ids"]).strip()
            or _COMPARISON_STATE.sub("", match["states"]).strip()
            or "".join(_compact_identity(value) for value in ids) != _compact_identity(match["names"])
        ):
            return match[0] + ". Unattributed commercial deployment is not currently verified."
        focal = [index for index, value in enumerate(ids) if _compact_identity(value) in _compact_identity(title)]
        if len(focal) != 1:
            return match[0] + ". Unattributed commercial deployment is not currently verified."
        state = states[focal[0]].casefold()
        if state == "generally available":
            # Raw prose, not a comparison row or reviewer claim, must provide
            # the affirmative deployment evidence.
            return "Attributed comparison status: current. Input price"
        if state == "not available":
            return "Attributed commercial deployment is not currently available. Input price"
        return f"Attributed commercial deployment is in {state}. Input price"
    original = str(text or "")
    result, count = _COMPARISON_TABLE.subn(bind, original)
    if not count and re.search(r"Developer\s*/\s*API\s+ID", original, re.I):
        result += "\nUnattributed commercial deployment is not currently verified."
    return result


def _classify_article_industrial_milestone(article):
    """Shared display/corroboration entry point with raw headline event scope."""
    title = str(article.get("title") or "")
    raw_text = "\n".join(str(article.get(field) or "") for field in ("title", "summary", "content"))
    primary_milestone = None
    if re.search(r"funding round|financing round|raised \$|融资|募集资金", title, re.I):
        primary_milestone = "funding"
    elif _ACQUISITION_EVENT.search(title):
        primary_milestone = "acquisition"
    model_names = tuple(match[0] for match in _MODEL_NAME.finditer(title))
    launch = _LAUNCHED_PRODUCT.search(title)
    subjects = model_names if model_names else ((launch[1],) if launch else ())
    # Comparison/model prose needs independently present raw deployment evidence;
    # reviewer summaries cannot fill in absent facts or ambiguous table cells.
    text = raw_text if subjects or re.search(r"Developer\s*/\s*API\s+ID", raw_text, re.I) else _article_evidence_text(article)
    return classify_industrial_milestone(
        _bind_comparison_status(text, title),
        event_type=(article.get("score_data") or {}).get("event_type"),
        audited_platform_aliases=_normalized_aliases(article, "audited_platform_aliases"),
        primary_milestone=primary_milestone,
        deployment_subjects=subjects,
        acquisition_completed=bool(_COMPLETED_ACQUISITION.search(title + "\n" + str(article.get("summary") or ""))),
    )


def _industrial_clauses(text):
    return [
        " ".join(clause.split())
        for clause in _INDUSTRIAL_CLAUSE_BOUNDARY.split(str(text or ""))
        if clause.strip()
    ]


def _current_clinical_readout(clauses):
    return bool(clauses) and all(
        _COMPLETED_CLINICAL_READOUT.search(clause)
        and not _UNAVAILABLE_CLINICAL_RESULTS.search(clause)
        and not _FUTURE_OR_NEGATED_STATE.search(clause)
        for clause in clauses
    )


def classify_industrial_milestone(
    text,
    *,
    event_type=None,
    audited_platform_aliases=(),
    primary_milestone=None,
    deployment_subjects=(),
    acquisition_completed=None,
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
            r"regulator approved|regulatory approvals?\b|granted (?:accelerated )?approval|"
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
            _CLINICAL_READOUT,
        ),
        (
            "commercial_deployment",
            r"commercial deployment|deployed to customers|entered service|"
            r"deployed\s+(?:more than\s+|over\s+)?\d[\d,]*(?:\+)?\s+"
            r"(?:agents|workloads)\b|"
            r"generally available|"
            r"\bavailable\s+(?:now\s+)?(?:through|via|in)\s+(?:the\s+)?"
            r"(?:[a-z0-9.-]+\s+){0,3}api\b|"
            r"\blive\s+(?:today|now)\s+(?:in|on|through)\s+(?:the\s+)?"
            r"(?:[a-z0-9.-]+\s+){0,3}api\b|"
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
        ("acquisition", _ACQUISITION_EVENT.pattern),
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
    if primary_milestone in {"acquisition", "funding"}:
        patterns = tuple(item for item in patterns if item[0] == primary_milestone) + tuple(
            item for item in patterns if item[0] != primary_milestone
        )
    if primary_milestone != "acquisition":
        # A background takeover is not the subject of every industrial story.
        patterns = tuple(item for item in patterns if item[0] != "acquisition")

    def deployment_bound(clause):
        return (
            not deployment_subjects
            or clause.startswith(("Attributed commercial", "Unattributed commercial"))
            # Unknown pronouns may constrain maturity, but must not supply
            # affirmative evidence borrowed from a preceding competitor.
            or (_ANONYMOUS_DEPLOYMENT_LIMIT.search(clause) and (
                _PRECOMMERCIAL_STATE.search(clause)
                or _NEGATED_DEPLOYMENT.search(clause)
                or _FUTURE_OR_NEGATED_STATE.search(clause)
            ))
            or any(
                re.search(re.escape(subject).replace(r"\-", "[- _]*").replace(r"\ ", "[- _]*"), clause, re.I)
                for subject in deployment_subjects
            )
        )
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
            if name == "commercial_deployment":
                matches = [clause for clause in matches if deployment_bound(clause)]
                for clause in clauses:
                    if not (_PRECOMMERCIAL_STATE.search(clause) or _NEGATED_DEPLOYMENT.search(clause)):
                        continue
                    named_subject = any(
                        re.search(re.escape(subject).replace(r"\-", "[- _]*").replace(r"\ ", "[- _]*"), clause, re.I)
                        for subject in deployment_subjects
                    )
                    if named_subject or _ANONYMOUS_DEPLOYMENT_LIMIT.search(clause):
                        matches.append(clause)
                if not matches:
                    continue
            if name == "regulatory_approval":
                event_matches = [
                    clause for clause in matches
                    if _COMPLETED_APPROVAL.search(clause)
                    or _FUTURE_OR_NEGATED_STATE.search(clause)
                    or _NEGATED_APPROVAL.search(clause)
                ]
                # A bare risk noun is not a separate event and must not
                # cancel an explicitly completed approval elsewhere.
                matches = event_matches or matches
                approval_current = all(
                    _COMPLETED_APPROVAL.search(clause)
                    and not _FUTURE_OR_NEGATED_STATE.search(clause)
                    and not _NEGATED_APPROVAL.search(clause)
                    for clause in matches
                )
                clinical = [clause for clause in clauses if re.search(_CLINICAL_READOUT, clause, re.I)]
                if not approval_current and _current_clinical_readout(clinical):
                    # Legal forward-looking approval boilerplate is not the
                    # event. Preserve the full original text, but classify its
                    # separately evidenced current clinical result as research.
                    milestone = "clinical_readout"
                    matched_clauses = clinical
                    break
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
    if milestone == "regulatory_approval":
        has_future_or_negation = has_future_or_negation or any(
            _NEGATED_APPROVAL.search(clause)
            or not _COMPLETED_APPROVAL.search(clause)
            for clause in matched_clauses
        )
    if milestone == "acquisition":
        has_future_or_negation = has_future_or_negation or any(
            _PENDING_ACQUISITION.search(clause) for clause in clauses
        )
    if milestone == "commercial_deployment":
        has_future_or_negation = has_future_or_negation or any(
            _NEGATED_DEPLOYMENT.search(clause) for clause in matched_clauses
        )
    has_precommercial_qualifier = any(
        _PRECOMMERCIAL_STATE.search(clause) for clause in matched_clauses
    )
    production_state = "none"
    if milestone != "none":
        if has_future_or_negation:
            production_state = "future"
        elif milestone == "clinical_readout" and not _current_clinical_readout(matched_clauses):
            production_state = "not_production"
        elif milestone == "acquisition":
            completed = acquisition_completed if acquisition_completed is not None else any(_COMPLETED_ACQUISITION.search(clause) for clause in matched_clauses)
            production_state = "current" if completed else "not_production"
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
