#!/usr/bin/env python3
"""Explicit, budgeted paid-model evaluation entrypoint."""

import argparse
import json
import os
from pathlib import Path
from dotenv import load_dotenv

from llm_cost_policy import write_telemetry
from main import load_config, save_json_atomic
from pipeline_scoring import score_articles_pipeline


CONFIRM_TOKEN = "RUN-BUDGETED-LLM-EVAL"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--articles", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("unattended", "deepseek"), required=True)
    parser.add_argument("--max-articles", type=int, required=True)
    parser.add_argument("--max-api-calls", type=int, required=True)
    parser.add_argument("--daily-budget-cny", type=float, required=True)
    parser.add_argument("--confirm-token", required=True)
    args = parser.parse_args(argv)
    if args.confirm_token != CONFIRM_TOKEN:
        raise SystemExit("invalid paid llm_eval confirmation token")
    if args.max_articles <= 0 or args.max_api_calls <= 0 or args.daily_budget_cny <= 0:
        raise SystemExit("llm_eval limits must all be positive")
    config = load_config(args.config)
    policy = config.setdefault("llm", {}).setdefault("policy", {})
    config["llm"]["mode"] = args.mode
    policy.update(
        {
            "allow_llm_eval": True,
            "max_articles_per_run": args.max_articles,
            "max_api_calls_per_run": args.max_api_calls,
            "daily_budget_cny": args.daily_budget_cny,
        }
    )
    os.environ["RADAR_LLM_MODE"] = args.mode
    os.environ["RADAR_LLM_EVAL"] = "1"
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    articles = json.loads(Path(args.articles).read_text(encoding="utf-8"))
    if not isinstance(articles, list):
        raise SystemExit("--articles must contain a JSON list")
    result = score_articles_pipeline(articles, config)
    output = Path(args.output).resolve()
    save_json_atomic(
        output,
        {
            "schema_version": 1,
            "component": "llm-eval-results",
            "articles": list(result.articles),
        },
    )
    write_telemetry(output.with_suffix(output.suffix + ".usage.json"), config)
    print(f"LLM_EVAL_RESULT={output}")


if __name__ == "__main__":
    main()
