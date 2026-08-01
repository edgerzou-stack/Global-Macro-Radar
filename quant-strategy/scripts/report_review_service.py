"""Optional report-review provider orchestration, independent of HTML rendering."""

from __future__ import annotations

import json
import time

from core.run_telemetry import metric_line
from report_review_cache import ReportReviewCache


DEFAULT_PROMPT_VERSION = "quant-report-review-v1"


def build_review_payload(strategies):
    """Return the bounded provider DTO; never forward internal strategy fields."""
    payload = {}
    for strategy, items in (strategies or {}).items():
        if not isinstance(items, list) or not items:
            continue
        slim_items = []
        for item in items[:10]:
            if not isinstance(item, dict):
                continue
            slim_items.append(
                {
                    "代码": item.get("股票代码", ""),
                    "简称": item.get("股票简称", ""),
                    "行业": item.get("所属行业", ""),
                    "市值": item.get("总市值", item.get("总市值(元)", "")),
                    "PE": item.get("PE", item.get("市盈率(TTM)", "")),
                    "最新价": item.get("最新价", ""),
                }
            )
        if slim_items:
            payload[str(strategy)] = slim_items
    return payload


def build_review_prompt(payload):
    return f"""作为资深量化基金经理，以下是各大子策略今日选出的 Top 10 股票核心指标：
{json.dumps(payload, ensure_ascii=False)}

请结合基本面常识，为每个策略分别给出质性评价。请严格以 JSON 格式返回，结构如下：
{{
  "strategy_reviews": {{
    "strategy_name": {{
      "reviews": [
        {{
          "股票代码": "代码",
          "股票简称": "简称",
          "护城河打分": 3.5,
          "成长性打分": 4.2,
          "一句话点评": "极短点评内容"
        }}
      ],
      "summary": "该策略总结"
    }}
  }}
}}
"""


def _valid_response(response):
    return (
        isinstance(response, dict)
        and isinstance(response.get("strategy_reviews"), dict)
        and bool(response["strategy_reviews"])
    )


def _default_metric_sink(component, counters, dimensions):
    print(
        metric_line(component, counters, dimensions=dimensions),
        flush=True,
    )


class ReportReviewService:
    """Own cache/provider/retry policy and return a renderer-neutral response."""

    def __init__(
        self,
        *,
        call_llm,
        configured_identities,
        prompt_version=DEFAULT_PROMPT_VERSION,
        cache=None,
        metric_sink=_default_metric_sink,
        sleep=time.sleep,
        max_retries=3,
        base_delay_seconds=5,
    ):
        self.call_llm = call_llm
        self.configured_identities = configured_identities
        self.prompt_version = str(prompt_version)
        self.cache = cache or ReportReviewCache()
        self.metric_sink = metric_sink
        self.sleep = sleep
        self.max_retries = int(max_retries)
        self.base_delay_seconds = float(base_delay_seconds)

    def _metric(self, **counters):
        self.metric_sink(
            "report_review_cache",
            counters,
            {"prompt_version": self.prompt_version},
        )

    def get_reviews(self, strategies):
        if not strategies or not self.call_llm:
            return {}
        payload = build_review_payload(strategies)
        if not payload:
            return {}

        for provider, model in self.configured_identities():
            cached = self.cache.read(
                payload,
                self.prompt_version,
                provider,
                model,
            )
            if _valid_response(cached):
                self._metric(hit=1, saved_external_calls=1)
                return cached
        self._metric(miss=1)

        prompt = build_review_prompt(payload)
        for attempt in range(self.max_retries):
            print(
                "Generating LLM batch reviews "
                f"(Attempt {attempt + 1}/{self.max_retries})...",
                flush=True,
            )
            try:
                response = self.call_llm(prompt, require_json=True)
                if _valid_response(response):
                    identity = response.get("_llm", {})
                    provider = (
                        identity.get("provider")
                        if isinstance(identity, dict)
                        else None
                    )
                    model = (
                        identity.get("model")
                        if isinstance(identity, dict)
                        else None
                    )
                    if provider and model and self.cache.write(
                        payload,
                        self.prompt_version,
                        provider,
                        model,
                        response,
                    ):
                        self._metric(write=1)
                    return response
                print(f"LLM returned invalid json for batch: {response}")
            except Exception as error:
                print(f"Failed to generate LLM batch reviews: {error}")

            if attempt + 1 < self.max_retries:
                self.sleep(self.base_delay_seconds * (2**attempt))
        return {}
