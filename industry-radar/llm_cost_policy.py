"""Run-bound LLM modes, budgets, manual review bundles, and usage telemetry."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


LLM_MODES = {"offline", "interactive", "unattended", "deepseek"}
TELEMETRY_SCHEMA_VERSION = 1
REVIEW_BUNDLE_SCHEMA_VERSION = 1
INTERACTIVE_REVIEW_BUNDLE_SCHEMA_VERSION = 4
INTERACTIVE_BASE_SCHEMA_VERSION = 1
UNSCORED_PRIORITY_SCORE_DATA = {"review_outcome": "unscored_priority"}
UNSCORED_PRIORITY_DISPLAY_FIELDS = {
    "translated_title",
    "translated_summary",
}
UNSCORED_PRIORITY_DISPLAY_SUMMARY_MAX_CHARACTERS = 50
_BEIJING = ZoneInfo("Asia/Shanghai")
AUTHORIZED_DAILY_TOPUP_CNY = 0.20


class LLMBudgetExceeded(RuntimeError):
    """The configured run/day budget forbids another external request."""


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _config_sha256(config):
    """Bind artifacts to configuration without run-time counters/paths."""

    stable = {key: value for key, value in config.items() if key != "_runtime"}
    return _sha256(stable)


def validate_unscored_priority_display(value):
    """Validate display-only Chinese copy for a scoreless priority item."""

    if not isinstance(value, dict) or set(value) != UNSCORED_PRIORITY_DISPLAY_FIELDS:
        raise ValueError("unscored priority display has invalid fields")
    display = {}
    for field in sorted(UNSCORED_PRIORITY_DISPLAY_FIELDS):
        text = value.get(field)
        if not isinstance(text, str) or not (text := text.strip()):
            raise ValueError(f"unscored priority display {field} must be non-empty")
        if not any("\u3400" <= char <= "\u9fff" for char in text):
            raise ValueError(
                f"unscored priority display {field} must contain Chinese text"
            )
        display[field] = text
    # Translation is display-only.  Match the regular scoring contract by
    # canonicalizing an overlong model response locally instead of spending a
    # second external request merely to enforce a presentational limit.
    display["translated_summary"] = display["translated_summary"][
        :UNSCORED_PRIORITY_DISPLAY_SUMMARY_MAX_CHARACTERS
    ]
    return display


def manual_review_response_contract():
    """Self-contained JSON contract consumed by folder-AI operators."""

    from event_contract import EVENT_TYPES

    return {
        "schema_version": 1,
        "top_level_fields": [
            "schema_version", "request_sha256", "reviewer", "scores"
        ],
        "reviewer_enum": ["gemini_ui", "codex_folder", "other_folder_ai"],
        "score_entry_fields": ["request_id", "score_data"],
        "unscored_priority_entry_fields": [
            "request_id", "score_data", "priority_display", "priority_industry_review"
        ],
        "priority_industry_review_fields": [
            "is_relevant", "event_type", "industrial_claims", "factual_basis", "reason"
        ],
        "unscored_priority_score_data": UNSCORED_PRIORITY_SCORE_DATA,
        "unscored_priority_display_fields": sorted(
            UNSCORED_PRIORITY_DISPLAY_FIELDS
        ),
        "unscored_priority_display_language": "Chinese",
        "unscored_priority_display_summary_max_characters": (
            UNSCORED_PRIORITY_DISPLAY_SUMMARY_MAX_CHARACTERS
        ),
        "score_data_required_fields": [
            "is_relevant", "is_vague_or_roundup", "event_type",
            "industrial_claims", "market_only_claims", "barrier_to_entry",
            "market_size", "immediacy", "reasoning_chain", "tech_score",
            "commercial_score", "hype_score", "macro_score", "justification",
            "translated_title", "translated_summary",
        ],
        "boolean_fields": ["is_relevant", "is_vague_or_roundup"],
        "string_list_fields": ["industrial_claims", "market_only_claims"],
        "string_fields": [
            "barrier_to_entry", "market_size", "immediacy", "reasoning_chain",
            "justification", "translated_title", "translated_summary",
        ],
        "integer_0_100_fields": [
            "tech_score", "commercial_score", "hype_score", "macro_score"
        ],
        "event_type_enum": sorted(EVENT_TYPES),
        "translated_summary_max_characters": 50,
        "cardinality": "each request_id exactly once; no missing or extra ids",
    }


def _positive_number(value, name, *, allow_zero=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        comparator = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be finite and {comparator}")
    return result


def _positive_integer(value, name, *, allow_zero=False):
    if type(value) is not int or value < 0 or (value == 0 and not allow_zero):
        comparator = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {comparator} integer")
    return value


@dataclass(frozen=True)
class LLMCostPolicy:
    mode: str
    daily_budget_cny: float
    enforce_daily_budget: bool
    max_articles_per_run: int
    max_api_calls_per_run: int
    estimated_cny_per_call: float
    allow_llm_eval: bool
    complete_review: bool = False

    @property
    def api_enabled(self):
        return self.mode in {"unattended", "deepseek"}


def resolve_policy(config, environ=None):
    environ = os.environ if environ is None else environ
    llm_config = config.get("llm", {})
    configured_mode = str(llm_config.get("mode", "offline")).strip().lower()
    mode = str(environ.get("RADAR_LLM_MODE", configured_mode)).strip().lower()
    if environ.get("PIPELINE_DISABLE_LLM") == "1":
        mode = "offline"
    if mode not in LLM_MODES:
        raise ValueError(
            f"RADAR_LLM_MODE must be one of {sorted(LLM_MODES)}, got {mode!r}"
        )
    settings = llm_config.get("policy", {})
    complete_review = settings.get("complete_review", False)
    if type(complete_review) is not bool:
        raise ValueError("llm.policy.complete_review must be boolean")
    if environ.get("RADAR_LLM_EVAL") == "1" and complete_review:
        raise ValueError("llm_eval cannot use complete_review")
    policy = LLMCostPolicy(
        mode=mode,
        daily_budget_cny=_positive_number(
            settings.get("daily_budget_cny", 2.0),
            "llm.policy.daily_budget_cny",
            allow_zero=True,
        ),
        enforce_daily_budget=not complete_review,
        max_articles_per_run=_positive_integer(
            settings.get("max_articles_per_run", 30),
            "llm.policy.max_articles_per_run",
        ),
        max_api_calls_per_run=_positive_integer(
            settings.get("max_api_calls_per_run", 20),
            "llm.policy.max_api_calls_per_run",
            allow_zero=True,
        ),
        estimated_cny_per_call=_positive_number(
            settings.get("estimated_cny_per_call", 0.05),
            "llm.policy.estimated_cny_per_call",
        ),
        allow_llm_eval=settings.get("allow_llm_eval", False) is True,
        complete_review=complete_review,
    )
    if environ.get("RADAR_LLM_EVAL") == "1":
        if not policy.allow_llm_eval:
            raise ValueError("RADAR_LLM_EVAL=1 requires llm.policy.allow_llm_eval=true")
        if not policy.api_enabled:
            raise ValueError("llm_eval requires unattended or deepseek mode")
    return policy


def policy_sha256(policy):
    return _sha256(asdict(policy))


def _resolve_authorized_daily_topup(policy, effective_date, environ):
    """Apply the explicitly supplied one-day top-up or reject it fail-closed."""

    raw_topup = environ.get("RADAR_LLM_AUTHORIZED_TOPUP_CNY")
    if raw_topup is None or not str(raw_topup).strip():
        return policy, None
    authorization_id = str(
        environ.get("RADAR_LLM_BUDGET_AUTHORIZATION_ID") or ""
    ).strip()
    authorization_date = str(
        environ.get("RADAR_LLM_BUDGET_AUTHORIZATION_DATE") or ""
    ).strip()
    if not authorization_id:
        raise ValueError(
            "RADAR_LLM_AUTHORIZED_TOPUP_CNY requires "
            "RADAR_LLM_BUDGET_AUTHORIZATION_ID"
        )
    if authorization_date != effective_date:
        raise ValueError(
            "RADAR_LLM_AUTHORIZED_TOPUP_CNY authorization date must match "
            "the pipeline effective date"
        )
    try:
        topup = float(str(raw_topup).strip())
    except ValueError as error:
        raise ValueError("RADAR_LLM_AUTHORIZED_TOPUP_CNY must be numeric") from error
    if not math.isfinite(topup) or abs(topup - AUTHORIZED_DAILY_TOPUP_CNY) > 1e-12:
        raise ValueError(
            "RADAR_LLM_AUTHORIZED_TOPUP_CNY must equal the approved CNY 0.20"
        )
    authorization = {
        "authorization_id": authorization_id,
        "authorization_date": authorization_date,
        "base_daily_budget_cny": policy.daily_budget_cny,
        "authorized_topup_cny": AUTHORIZED_DAILY_TOPUP_CNY,
        "authorized_daily_budget_cny": round(
            policy.daily_budget_cny + AUTHORIZED_DAILY_TOPUP_CNY, 8
        ),
    }
    return (
        replace(
            policy,
            daily_budget_cny=authorization["authorized_daily_budget_cny"],
        ),
        authorization,
    )


def _resolve_authorized_no_daily_budget(policy, effective_date, environ):
    """Disable only the daily monetary cap for an explicitly authorized run.

    The per-run request ceiling remains in force, and every request is still
    reserved in the immutable daily ledger for cost auditability.
    """

    raw_override = environ.get("RADAR_LLM_DISABLE_DAILY_BUDGET")
    if raw_override is None or not str(raw_override).strip():
        return policy, None
    if str(raw_override).strip() != "1":
        raise ValueError("RADAR_LLM_DISABLE_DAILY_BUDGET must equal 1")
    authorization_id = str(
        environ.get("RADAR_LLM_UNLIMITED_AUTHORIZATION_ID") or ""
    ).strip()
    authorization_date = str(
        environ.get("RADAR_LLM_UNLIMITED_AUTHORIZATION_DATE") or ""
    ).strip()
    if not authorization_id:
        raise ValueError(
            "RADAR_LLM_DISABLE_DAILY_BUDGET requires "
            "RADAR_LLM_UNLIMITED_AUTHORIZATION_ID"
        )
    if authorization_date != effective_date:
        raise ValueError(
            "RADAR_LLM_DISABLE_DAILY_BUDGET authorization date must match "
            "the pipeline effective date"
        )
    return (
        replace(policy, enforce_daily_budget=False),
        {
            "authorization_id": authorization_id,
            "authorization_date": authorization_date,
            "daily_budget_enforced": False,
            "per_run_api_call_limit": policy.max_api_calls_per_run,
        },
    )


class LLMCostRun:
    def __init__(
        self,
        policy,
        *,
        run_id,
        effective_date,
        ledger_path,
        budget_authorization=None,
    ):
        self.policy = policy
        self.run_id = str(run_id)
        self.effective_date = str(effective_date)
        self.ledger_path = Path(ledger_path).resolve()
        self.budget_authorization = (
            dict(budget_authorization)
            if isinstance(budget_authorization, dict)
            else None
        )
        self._lock = threading.Lock()
        self._thread_state = threading.local()
        self._admitted_article_keys = set()
        self._metrics = {
            "cache_hit_count": 0,
            "cache_miss_count": 0,
            "priority_display_cache_hit_count": 0,
            "priority_display_cache_miss_count": 0,
            "deterministic_count": 0,
            "ai_review_count": 0,
            "api_call_count": 0,
            "estimated_cost_cny": 0.0,
            "budget_blocked_count": 0,
            "manual_review_count": 0,
            "new_manual_review_count": 0,
            "reused_manual_review_count": 0,
        }

    def increment(self, name, amount=1):
        if name not in self._metrics:
            raise KeyError(name)
        with self._lock:
            self._metrics[name] += amount

    def admit_articles(self, article_keys):
        admitted = []
        blocked = []
        with self._lock:
            for key in article_keys:
                if key in self._admitted_article_keys:
                    admitted.append(key)
                    continue
                if (
                    not self.policy.complete_review
                    and len(self._admitted_article_keys)
                    >= self.policy.max_articles_per_run
                ):
                    blocked.append(key)
                    self._metrics["budget_blocked_count"] += 1
                    continue
                self._admitted_article_keys.add(key)
                self._metrics["ai_review_count"] += 1
                admitted.append(key)
        return set(admitted), set(blocked)

    def _reserve_daily_call(self, provider, operation):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.ledger_path.with_suffix(self.ledger_path.suffix + ".lock")
        with open(lock_path, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    ledger = {}
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise RuntimeError(f"Cannot read LLM budget ledger: {error}") from error
                if ledger and (
                    ledger.get("effective_date") != self.effective_date
                    or ledger.get("schema_version") != 1
                ):
                    raise RuntimeError("LLM budget ledger identity mismatch")
                spent = float(ledger.get("estimated_cost_cny", 0.0) or 0.0)
                projected = spent + self.policy.estimated_cny_per_call
                if (
                    self.policy.enforce_daily_budget
                    and projected > self.policy.daily_budget_cny + 1e-12
                ):
                    return False
                calls = int(ledger.get("api_call_count", 0) or 0) + 1
                entries = list(ledger.get("reservations") or [])
                entries.append(
                    {
                        "run_id": self.run_id,
                        "provider": str(provider),
                        "operation": str(operation),
                        "estimated_cost_cny": self.policy.estimated_cny_per_call,
                    }
                )
                updated = {
                    "schema_version": 1,
                    "effective_date": self.effective_date,
                    "daily_budget_enforced": self.policy.enforce_daily_budget,
                    "api_call_count": calls,
                    "estimated_cost_cny": round(projected, 8),
                    "reservations": entries,
                }
                _write_json_atomic(self.ledger_path, updated)
                return True
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def authorize_api_call(self, provider, operation):
        if not self.policy.api_enabled:
            raise LLMBudgetExceeded(
                f"LLM API calls are forbidden in {self.policy.mode} mode"
            )
        with self._lock:
            if (
                not self.policy.complete_review
                and self._metrics["api_call_count"]
                >= self.policy.max_api_calls_per_run
            ):
                self._metrics["budget_blocked_count"] += 1
                raise LLMBudgetExceeded("LLM per-run API call budget exhausted")
        if not self._reserve_daily_call(provider, operation):
            with self._lock:
                self._metrics["budget_blocked_count"] += 1
            raise LLMBudgetExceeded("LLM daily CNY budget exhausted")
        with self._lock:
            self._metrics["api_call_count"] += 1
            self._metrics["estimated_cost_cny"] = round(
                self._metrics["estimated_cost_cny"]
                + self.policy.estimated_cny_per_call,
                8,
            )

    def mark_next_router_call_preauthorized(self):
        self._thread_state.preauthorized = True

    def consume_router_preauthorization(self):
        if getattr(self._thread_state, "preauthorized", False):
            self._thread_state.preauthorized = False
            return True
        return False

    def has_initial_api_capacity(self):
        return bool(
            self.policy.api_enabled
            and (self.policy.complete_review or self.policy.max_api_calls_per_run > 0)
            and (
                not self.policy.enforce_daily_budget
                or self.policy.daily_budget_cny + 1e-12
                >= self.policy.estimated_cny_per_call
            )
        )

    def snapshot(self):
        with self._lock:
            metrics = dict(self._metrics)
        snapshot = {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "component": "radar-llm-cost",
            "run_id": self.run_id,
            "effective_date": self.effective_date,
            "mode": self.policy.mode,
            "policy": asdict(self.policy),
            "policy_sha256": policy_sha256(self.policy),
            **metrics,
        }
        if self.budget_authorization is not None:
            snapshot["budget_authorization"] = self.budget_authorization
        return snapshot


_ACTIVE_RUN = None
_ACTIVE_LOCK = threading.Lock()


def _resolve_run_identity(explicit, environ, primary_name, legacy_name, default):
    candidates = [
        str(value).strip()
        for value in (
            explicit,
            environ.get(primary_name),
            environ.get(legacy_name),
        )
        if value is not None and str(value).strip()
    ]
    if len(set(candidates)) > 1:
        raise ValueError(
            f"conflicting {primary_name}/{legacy_name} identity values"
        )
    return candidates[0] if candidates else default


def start_run(config, *, run_id=None, effective_date=None, environ=None):
    global _ACTIVE_RUN
    environ = os.environ if environ is None else environ
    policy = resolve_policy(config, environ)
    effective_date = _resolve_run_identity(
        effective_date,
        environ,
        "PIPELINE_EFFECTIVE_DATE",
        "EFFECTIVE_DATE",
        datetime.now(_BEIJING).date().isoformat(),
    )
    try:
        datetime.strptime(effective_date, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(
            f"invalid pipeline effective date {effective_date!r}"
        ) from error
    policy, budget_authorization = _resolve_authorized_daily_topup(
        policy, effective_date, environ
    )
    policy, unlimited_authorization = _resolve_authorized_no_daily_budget(
        policy, effective_date, environ
    )
    if budget_authorization is not None and unlimited_authorization is not None:
        raise ValueError(
            "RADAR_LLM_AUTHORIZED_TOPUP_CNY cannot be combined with "
            "RADAR_LLM_DISABLE_DAILY_BUDGET"
        )
    budget_authorization = unlimited_authorization or budget_authorization
    run_id = _resolve_run_identity(
        run_id,
        environ,
        "PIPELINE_RUN_ID",
        "RUN_ID",
        "standalone",
    )
    ledger = environ.get("RADAR_LLM_BUDGET_LEDGER")
    if not ledger:
        ledger = str(
            Path(__file__).resolve().parent
            / ".cache"
            / f"llm-budget-{effective_date}.json"
        )
    with _ACTIVE_LOCK:
        _ACTIVE_RUN = LLMCostRun(
            policy,
            run_id=run_id,
            effective_date=effective_date,
            ledger_path=ledger,
            budget_authorization=budget_authorization,
        )
    config.setdefault("_runtime", {})["llm_mode"] = policy.mode
    return _ACTIVE_RUN


def active_run(config=None):
    global _ACTIVE_RUN
    with _ACTIVE_LOCK:
        current = _ACTIVE_RUN
    if current is None:
        if config is None:
            raise RuntimeError("LLM cost run has not been initialized")
        current = start_run(config)
    return current


def reset_active_run():
    global _ACTIVE_RUN
    with _ACTIVE_LOCK:
        _ACTIVE_RUN = None


def record_runtime(config, controller=None):
    controller = controller or active_run(config)
    snapshot = controller.snapshot()
    config.setdefault("_runtime", {})["llm_cost"] = {
        key: snapshot[key]
        for key in (
            "cache_hit_count",
            "cache_miss_count",
            "priority_display_cache_hit_count",
            "priority_display_cache_miss_count",
            "deterministic_count",
            "ai_review_count",
            "api_call_count",
            "estimated_cost_cny",
            "budget_blocked_count",
            "manual_review_count",
            "new_manual_review_count",
            "reused_manual_review_count",
            "mode",
        )
    }
    return snapshot


def _write_json_atomic(path, payload):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_telemetry(path, config, controller=None):
    payload = record_runtime(config, controller)
    _write_json_atomic(path, payload)
    return str(Path(path).resolve())


def write_manual_review_bundle(
    path,
    articles,
    *,
    config,
    prompt_version,
    rules_sha256,
    semantic_key_for,
    controller=None,
    rss_fixture_path=None,
    base_scores_path=None,
):
    from score import build_scoring_rubric, scoring_rubric_sha256

    controller = controller or active_run(config)
    rubric = build_scoring_rubric(config)
    rubric_sha256 = scoring_rubric_sha256(config)
    complete_interactive_bundle = (
        rss_fixture_path is not None and base_scores_path is not None
    )
    entries = []
    for article in articles:
        semantic_key = semantic_key_for(article)
        content = str(article.get("content") or "")
        summary = str(article.get("summary") or "")
        if content.strip():
            content_basis = "feed_body"
            review_text = content
        elif summary.strip():
            content_basis = "summary_only"
            review_text = summary
        else:
            raise ValueError(
                f"manual review article {semantic_key} has no auditable review text"
            )
        entry = {
                "request_id": semantic_key,
                "semantic_input_sha256": semantic_key,
                "title": str(article.get("title") or ""),
                "summary": summary,
                "content": content,
                "link": str(article.get("link") or article.get("url") or ""),
                "published_at": str(article.get("published_at") or ""),
                "source": str(article.get("source") or ""),
                "source_id": str(article.get("source_id") or ""),
                "source_tier": str(article.get("source_tier") or ""),
                "source_lane": str(article.get("source_lane") or ""),
                "authority_for": sorted(article.get("authority_for") or []),
            }
        if complete_interactive_bundle:
            entry["content_basis"] = content_basis
            entry["review_text"] = review_text
        entries.append(entry)
    entries.sort(key=lambda item: item["request_id"])
    request_identity = {
        "prompt_version": str(prompt_version),
        "rules_sha256": str(rules_sha256),
        "rubric": rubric,
        "rubric_sha256": rubric_sha256,
        "articles": entries,
    }
    schema_version = REVIEW_BUNDLE_SCHEMA_VERSION
    artifact_bindings = {}
    if rss_fixture_path is not None or base_scores_path is not None:
        if rss_fixture_path is None or base_scores_path is None:
            raise ValueError(
                "interactive request requires both RSS and base-score artifacts"
            )
        request_dir = Path(path).resolve().parent
        rss_path = Path(rss_fixture_path).resolve()
        base_path = Path(base_scores_path).resolve()
        if rss_path.parent != request_dir or base_path.parent != request_dir:
            raise ValueError(
                "interactive artifacts must share the request directory"
            )
        schema_version = INTERACTIVE_REVIEW_BUNDLE_SCHEMA_VERSION
        artifact_bindings = {
            "config_sha256": _config_sha256(config),
            "no_manual_review_needed": not entries,
            "response_contract": manual_review_response_contract(),
            "rss_fixture": {
                "name": rss_path.name,
                "sha256": _file_sha256(rss_path),
            },
            "base_scores": {
                "name": base_path.name,
                "sha256": _file_sha256(base_path),
            },
        }
        request_identity.update(
            {
                "run_id": controller.run_id,
                "effective_date": controller.effective_date,
                **artifact_bindings,
            }
        )
    payload = {
        "schema_version": schema_version,
        "run_id": controller.run_id,
        "effective_date": controller.effective_date,
        "mode": "interactive",
        "prompt_version": str(prompt_version),
        "rules_sha256": str(rules_sha256),
        "rubric": rubric,
        "rubric_sha256": rubric_sha256,
        "request_sha256": _sha256(request_identity),
        "item_count": len(entries),
        "articles": entries,
        **artifact_bindings,
    }
    _write_json_atomic(path, payload)
    controller.increment("manual_review_count", len(entries))
    controller.increment("new_manual_review_count", len(entries))
    config.setdefault("_runtime", {})["llm_review_bundle_path"] = str(
        Path(path).resolve()
    )
    return payload


def write_interactive_rss_fixture(path, articles, health, *, reference_time=None, run_id=None):
    """Seal the complete ingested input used by a folder-AI review run."""

    clean_articles = []
    seen_links = set()
    for index, source in enumerate(articles):
        article = {
            key: value
            for key, value in dict(source).items()
            if key not in {"id", "score_data"} and not str(key).startswith("_")
        }
        link = article.get("link")
        if not isinstance(link, str) or not link.strip() or link in seen_links:
            raise ValueError(
                f"interactive RSS article {index} has invalid/duplicate link"
            )
        seen_links.add(link)
        clean_articles.append(article)

    clean_health = [dict(item) for item in health]
    fresh_total = sum(int(item.get("fresh_entries", 0) or 0) for item in clean_health)
    # Ingestion deduplicates URLs before this hand-off. Preserve the source's
    # observed count while making the sealed fixture replayable by load_rss_fixture.
    excess = fresh_total - len(clean_articles)
    if excess < 0:
        raise ValueError("RSS health fresh count is smaller than the article set")
    for item in reversed(clean_health):
        if excess <= 0:
            break
        fresh = int(item.get("fresh_entries", 0) or 0)
        reduction = min(fresh, excess)
        if reduction:
            item["captured_fresh_entries"] = fresh
            item["fresh_entries"] = fresh - reduction
            item["fresh"] = item["fresh_entries"] > 0
            excess -= reduction
    if excess:
        raise ValueError("cannot normalize RSS health to the deduplicated article set")

    payload = {
        "schema_version": 1,
        "articles": clean_articles,
        "health": clean_health,
    }
    if reference_time is not None:
        from pipeline_health import aware_utc_timestamp, validate_rss_capture_time

        captured = aware_utc_timestamp(reference_time, "reference_time")
        validate_rss_capture_time(clean_articles, clean_health, captured)
        payload["capture_clock"] = {
            "run_id": run_id,
            "reference_time": captured.isoformat(),
        }
    _write_json_atomic(path, payload)
    return {
        "path": str(Path(path).resolve()),
        "sha256": _file_sha256(path),
        "article_count": len(clean_articles),
    }


def write_interactive_base_scores(
    path,
    articles,
    *,
    config,
    prompt_version,
    rules_sha256,
    rss_fixture_path,
    controller=None,
):
    """Seal cache/deterministic results and placeholders for manual results."""

    controller = controller or active_run(config)
    rss_path = Path(rss_fixture_path).resolve()
    rows = []
    seen_links = set()
    for index, article in enumerate(articles):
        link = article.get("link")
        if not isinstance(link, str) or not link.strip() or link in seen_links:
            raise ValueError(
                f"interactive base score {index} has invalid/duplicate link"
            )
        seen_links.add(link)
        resolution = str(article.get("_score_resolution") or "")
        if resolution not in {"cache", "deterministic", "manual"}:
            raise ValueError(f"invalid interactive score resolution {resolution!r}")
        score_data = article.get("score_data")
        if resolution == "manual":
            score_data = None
        elif not isinstance(score_data, dict):
            raise ValueError(f"resolved base score for {link} is missing")
        rows.append(
            {
                "link": link,
                "semantic_input_sha256": str(article.get("_cache_key") or ""),
                "resolution": resolution,
                "score_data": score_data,
            }
        )
    payload = {
        "schema_version": INTERACTIVE_BASE_SCHEMA_VERSION,
        "component": "interactive-scoring-base",
        "run_id": controller.run_id,
        "effective_date": controller.effective_date,
        "config_sha256": _config_sha256(config),
        "prompt_version": str(prompt_version),
        "rules_sha256": str(rules_sha256),
        "rss_fixture": {
            "name": rss_path.name,
            "sha256": _file_sha256(rss_path),
        },
        "scores": rows,
    }
    _write_json_atomic(path, payload)
    return payload
