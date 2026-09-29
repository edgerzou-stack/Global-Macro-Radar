#!/usr/bin/env python3
"""Offline import/configuration smoke check for the public Industry Radar tree."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# The smoke check must be observational: importing the candidate cannot add
# __pycache__ files that would make the exact release tree fail its next audit.
sys.dont_write_bytecode = True


def run_smoke(root):
    root = root.resolve()
    radar = root / "industry-radar"
    config_path = radar / "config.example.yaml"
    if not radar.is_dir() or not config_path.is_file():
        raise RuntimeError("public Industry Radar files are incomplete")
    if (root / "quant-strategy").exists():
        raise RuntimeError("private quant-strategy directory is present")
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(radar))
    os.environ["RADAR_CONFIG"] = str(config_path)
    os.environ["PIPELINE_DISABLE_LLM"] = "1"
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["OPENAI_API_KEY"] = ""
    os.environ["DEEPSEEK_API_KEY"] = ""
    import yaml

    import event_contract
    import cache_manager
    import evidence_policy
    import main as radar_main
    import pipeline_selection
    import score
    import source_registry

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if cache_manager.is_revoked_manual_score({"manual_review_provenance": {"run_id": "public-smoke"}}):
        raise RuntimeError("public smoke manual score unexpectedly revoked")
    registry = source_registry.load_source_registry(config)
    rss_registry = [
        entry for entry in registry.values() if entry.get("adapter") == "rss"
    ]
    newsroom_registry = [
        entry
        for entry in registry.values()
        if entry.get("adapter") == "official_newsroom"
    ]
    if len(rss_registry) != len(config.get("rss_feeds", [])):
        raise RuntimeError("source registry and RSS configuration are not aligned")
    if len(newsroom_registry) != len(config.get("official_newsrooms", [])):
        raise RuntimeError(
            "source registry and official newsroom configuration are not aligned"
        )
    providers = config.get("llm", {}).get("providers", {})
    enabled_providers = sorted(
        name for name, provider in providers.items() if provider.get("enabled") is True
    )
    if enabled_providers:
        raise RuntimeError(
            "public example must disable external LLM providers by default: "
            + ", ".join(enabled_providers)
        )
    if config.get("delivery", {}).get("enabled") is not False:
        raise RuntimeError("public example must disable email delivery by default")
    decision = evidence_policy.research_watch_decision(
        {
            "title": "Prototype completes engineering test",
            "summary": "A new optical engine prototype completed an engineering test.",
            "event_type": "prototype",
            "source_tier": "T1",
            "source_lane": "research",
            "production_state": "future",
        }
    )
    if not isinstance(decision, dict) or "eligible" not in decision:
        raise RuntimeError("Research Watch decision contract is unavailable")
    return {
        "status": "ok",
        "manual_cache_policy": True,
        "rss_sources": len(rss_registry),
        "official_newsroom_sources": len(newsroom_registry),
        "event_types": len(event_contract.INDUSTRIAL_EVENT_TYPES),
        "external_providers_enabled": enabled_providers,
        "delivery_enabled": False,
        "scoring_prompt_version": score.SCORING_PROMPT_VERSION,
        "main_module": radar_main.__name__,
        "selection_module": pipeline_selection.__name__,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Public candidate root")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run_smoke(Path(args.root))
    except Exception as error:
        print(f"public radar smoke check failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
