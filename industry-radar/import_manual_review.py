#!/usr/bin/env python3
"""Validate an audited folder-agent review and compile a scored fixture."""

import argparse
import hashlib
import json
from pathlib import Path

from llm_cost_policy import (
    _canonical_json,
    _write_json_atomic,
    manual_review_response_contract,
)
from cache_manager import make_cache_entry, merge_verified_cache_entries
from score import _validate_score_result


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_json_object(path, label):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load {label}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _request_identity(request):
    identity = {
        "prompt_version": request.get("prompt_version"),
        "rules_sha256": request.get("rules_sha256"),
        "articles": request.get("articles"),
    }
    if request.get("schema_version") in {2, 3}:
        identity.update(
            {
                "run_id": request.get("run_id"),
                "effective_date": request.get("effective_date"),
                "config_sha256": request.get("config_sha256"),
                "no_manual_review_needed": request.get("no_manual_review_needed"),
                "response_contract": request.get("response_contract"),
                "rss_fixture": request.get("rss_fixture"),
                "base_scores": request.get("base_scores"),
            }
        )
    return identity


def _validate_manual_response(request, response):
    if set(response) != {"schema_version", "request_sha256", "reviewer", "scores"}:
        raise ValueError("manual review response has invalid top-level fields")
    if response.get("schema_version") != 1:
        raise ValueError("unsupported manual review response schema")
    if response.get("request_sha256") != request.get("request_sha256"):
        raise ValueError("manual review response does not bind the exact request")
    reviewer = str(response.get("reviewer") or "")
    if reviewer not in {"gemini_ui", "codex_folder", "other_folder_ai"}:
        raise ValueError("manual reviewer must be an audited folder-AI identity")
    articles = request.get("articles")
    scores = response.get("scores")
    if not isinstance(articles, list) or not isinstance(scores, list):
        raise ValueError("manual review articles and scores must be lists")
    expected_article_fields = {
        "request_id", "semantic_input_sha256", "title", "summary", "content",
        "link", "published_at", "source", "source_id", "source_tier",
        "source_lane", "authority_for",
    }
    if request.get("schema_version") == 3:
        expected_article_fields.update({"content_basis", "review_text"})
    for index, item in enumerate(articles):
        if not isinstance(item, dict) or set(item) != expected_article_fields:
            raise ValueError(f"manual review request article {index} has invalid fields")
        if item.get("request_id") != item.get("semantic_input_sha256"):
            raise ValueError("manual review request semantic identity mismatch")
        if request.get("schema_version") == 3:
            content = str(item.get("content") or "")
            summary = str(item.get("summary") or "")
            expected_basis = "feed_body" if content.strip() else "summary_only"
            expected_text = content if content.strip() else summary
            if (
                not expected_text.strip()
                or item.get("content_basis") != expected_basis
                or item.get("review_text") != expected_text
            ):
                raise ValueError(
                    f"manual review request article {index} review material mismatch"
                )
    article_by_id = {item.get("request_id"): item for item in articles}
    if len(article_by_id) != len(articles) or None in article_by_id:
        raise ValueError("manual review request ids must be unique")
    score_by_id = {}
    for index, item in enumerate(scores):
        if not isinstance(item, dict) or set(item) != {"request_id", "score_data"}:
            raise ValueError(f"manual score {index} has invalid fields")
        request_id = item.get("request_id")
        if request_id in score_by_id:
            raise ValueError("manual review response contains a duplicate request_id")
        score_data = item.get("score_data")
        if request.get("schema_version") in {2, 3}:
            required = set(
                request["response_contract"]["score_data_required_fields"]
            )
            if not isinstance(score_data, dict) or set(score_data) != required:
                raise ValueError(
                    f"manual score {index} score_data fields do not match contract"
                )
        score_by_id[request_id] = _validate_score_result(dict(score_data))
    if set(score_by_id) != set(article_by_id):
        raise ValueError("manual review response request set mismatch")
    return reviewer, article_by_id, score_by_id


def _bound_artifact(request_path, binding, label):
    if not isinstance(binding, dict) or set(binding) != {"name", "sha256"}:
        raise ValueError(f"manual review request has invalid {label} binding")
    name = binding.get("name")
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ValueError(f"manual review request has unsafe {label} name")
    path = request_path.parent / name
    if not path.is_file() or _sha256_file(path) != binding.get("sha256"):
        raise ValueError(f"manual review {label} hash mismatch")
    return path


def _compile_complete_fixture(request_path, request, article_by_id, score_by_id):
    rss_path = _bound_artifact(request_path, request.get("rss_fixture"), "RSS fixture")
    base_path = _bound_artifact(request_path, request.get("base_scores"), "base scores")
    rss = _load_json_object(rss_path, "RSS fixture")
    base = _load_json_object(base_path, "base scores")
    required_rss = {"schema_version", "articles", "health"}
    if set(rss) not in (required_rss, required_rss | {"capture_clock"}) or rss.get("schema_version") != 1:
        raise ValueError("interactive RSS fixture has invalid schema")
    rss_articles = rss.get("articles")
    if not isinstance(rss_articles, list):
        raise ValueError("interactive RSS fixture articles must be a list")
    rss_links = [item.get("link") if isinstance(item, dict) else None for item in rss_articles]
    if any(not isinstance(link, str) or not link for link in rss_links) or len(set(rss_links)) != len(rss_links):
        raise ValueError("interactive RSS fixture links must be unique and non-empty")
    expected_base_fields = {
        "schema_version", "component", "run_id", "effective_date",
        "config_sha256", "prompt_version", "rules_sha256", "rss_fixture", "scores",
    }
    if set(base) != expected_base_fields or base.get("schema_version") != 1 or base.get("component") != "interactive-scoring-base":
        raise ValueError("interactive base scores have invalid schema")
    for field in (
        "run_id", "effective_date", "config_sha256", "prompt_version", "rules_sha256"
    ):
        if base.get(field) != request.get(field):
            raise ValueError(f"interactive base/request {field} mismatch")
    if base.get("rss_fixture") != request.get("rss_fixture"):
        raise ValueError("interactive base/request RSS binding mismatch")
    rows = base.get("scores")
    if not isinstance(rows, list):
        raise ValueError("interactive base scores must be a list")
    base_by_link = {}
    manual_by_id = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "link", "semantic_input_sha256", "resolution", "score_data"
        }:
            raise ValueError(f"interactive base score {index} has invalid fields")
        link = row.get("link")
        if not isinstance(link, str) or not link or link in base_by_link:
            raise ValueError("interactive base scores contain invalid/duplicate link")
        resolution = row.get("resolution")
        if resolution not in {"cache", "deterministic", "manual"}:
            raise ValueError("interactive base score has invalid resolution")
        if resolution == "manual":
            if row.get("score_data") is not None:
                raise ValueError("manual base score must be an unresolved placeholder")
            request_id = row.get("semantic_input_sha256")
            if request_id in manual_by_id:
                raise ValueError("interactive base scores duplicate a manual request id")
            manual_by_id[request_id] = link
        else:
            if not isinstance(row.get("score_data"), dict):
                raise ValueError("resolved base score must contain score_data")
            _validate_score_result(dict(row["score_data"]))
        base_by_link[link] = row
    if set(base_by_link) != set(rss_links):
        raise ValueError("interactive base score URL set does not match complete RSS")
    request_manual = {
        request_id: item.get("link") for request_id, item in article_by_id.items()
    }
    if manual_by_id != request_manual:
        raise ValueError("manual request set does not match base placeholders")
    compiled = []
    for link in rss_links:
        row = base_by_link[link]
        score_data = (
            score_by_id[row["semantic_input_sha256"]]
            if row["resolution"] == "manual"
            else row["score_data"]
        )
        compiled.append({"link": link, "score_data": score_data})
    return {"schema_version": 1, "scores": compiled}, rss_path, base_path


def _persist_verified_manual_scores(
    request,
    score_by_id,
    receipt,
    receipt_path,
    request_path,
    response_path,
    output_path,
):
    """Persist only scores that passed the complete manual-import contract."""

    if request.get("schema_version") not in {2, 3} or not score_by_id:
        return 0
    provenance_common = {
        "schema_version": 1,
        "source_run_id": request.get("run_id"),
        "source_effective_date": request.get("effective_date"),
        "request_sha256": request.get("request_sha256"),
        "request_file_sha256": receipt.get("request_file_sha256"),
        "request_path": str(Path(request_path).resolve()),
        "response_file_sha256": receipt.get("response_file_sha256"),
        "response_path": str(Path(response_path).resolve()),
        "receipt_file_sha256": _sha256_file(receipt_path),
        "receipt_path": str(Path(receipt_path).resolve()),
        "output_sha256": receipt.get("output_sha256"),
        "output_path": str(Path(output_path).resolve()),
        "reviewer": receipt.get("reviewer"),
        "prompt_version": request.get("prompt_version"),
        "rules_sha256": request.get("rules_sha256"),
        "config_sha256": request.get("config_sha256"),
        "response_contract_sha256": hashlib.sha256(
            _canonical_json(request.get("response_contract")).encode("utf-8")
        ).hexdigest(),
    }
    entries = {}
    for semantic_key, score_data in score_by_id.items():
        score_sha256 = hashlib.sha256(
            _canonical_json(score_data).encode("utf-8")
        ).hexdigest()
        provenance = {
            **provenance_common,
            "semantic_input_sha256": semantic_key,
            "score_data_sha256": score_sha256,
        }
        entries[semantic_key] = make_cache_entry(
            semantic_key,
            score_data,
            provider="manual_review",
            model=str(receipt.get("reviewer") or "unknown"),
            semantic_cache_key=semantic_key,
            rules_sha256=request.get("rules_sha256"),
            prompt_version=request.get("prompt_version"),
            manual_review_provenance=provenance,
        )
    merge_verified_cache_entries(entries)
    return len(entries)


def import_review(
    request_path,
    response_path,
    output_path,
    *,
    persist_verified_cache=False,
):
    request_path = Path(request_path).resolve()
    response_path = Path(response_path).resolve()
    output_path = Path(output_path).resolve()
    request = _load_json_object(request_path, "manual review request")
    response = _load_json_object(response_path, "manual review response")
    if request.get("schema_version") not in {1, 2, 3} or request.get("mode") != "interactive":
        raise ValueError("unsupported manual review request")
    if request.get("schema_version") in {2, 3}:
        expected_request_fields = {
            "schema_version", "run_id", "effective_date", "mode",
            "prompt_version", "rules_sha256", "request_sha256", "item_count",
            "articles", "config_sha256", "no_manual_review_needed",
            "response_contract",
            "rss_fixture", "base_scores",
        }
        if set(request) != expected_request_fields:
            raise ValueError("manual review request has invalid top-level fields")
        if request.get("item_count") != len(request.get("articles") or []):
            raise ValueError("manual review request item_count mismatch")
        if type(request.get("no_manual_review_needed")) is not bool:
            raise ValueError("manual review request has invalid no-manual flag")
        if request["no_manual_review_needed"] is not (request["item_count"] == 0):
            raise ValueError("manual review request no-manual flag mismatch")
        if request.get("response_contract") != manual_review_response_contract():
            raise ValueError("manual review request response contract mismatch")
    expected_request_sha = hashlib.sha256(
        _canonical_json(_request_identity(request)).encode("utf-8")
    ).hexdigest()
    if request.get("request_sha256") != expected_request_sha:
        raise ValueError("manual review request content hash mismatch")
    reviewer, article_by_id, score_by_id = _validate_manual_response(request, response)
    if request.get("schema_version") in {2, 3}:
        fixture, rss_path, base_path = _compile_complete_fixture(
            request_path, request, article_by_id, score_by_id
        )
    else:
        fixture = {
            "schema_version": 1,
            "scores": [
                {
                    "link": article_by_id[request_id]["link"],
                    "score_data": score_by_id[request_id],
                }
                for request_id in sorted(article_by_id)
            ],
        }
        rss_path = base_path = None
    _write_json_atomic(output_path, fixture)
    receipt = {
        "schema_version": 1,
        "component": "manual-review-import",
        "request_sha256": request["request_sha256"],
        "request_file_sha256": _sha256_file(request_path),
        "response_file_sha256": _sha256_file(response_path),
        "reviewer": reviewer,
        "run_id": request.get("run_id"),
        "effective_date": request.get("effective_date"),
        "manual_item_count": len(score_by_id),
        "total_item_count": len(fixture["scores"]),
        "output": str(output_path),
        "output_sha256": _sha256_file(output_path),
        "validation": "passed",
    }
    if rss_path is not None:
        receipt["rss_fixture_sha256"] = _sha256_file(rss_path)
        receipt["base_scores_sha256"] = _sha256_file(base_path)
    if persist_verified_cache:
        receipt["verified_cache_persisted_count"] = len(score_by_id)
    receipt_path = output_path.with_suffix(output_path.suffix + ".receipt.json")
    _write_json_atomic(receipt_path, receipt)
    if persist_verified_cache:
        _persist_verified_manual_scores(
            request,
            score_by_id,
            receipt,
            receipt_path,
            request_path,
            response_path,
            output_path,
        )
    return fixture, receipt_path


def compile_without_manual_review(request_path, output_path):
    """Compile a fully resolved interactive base without inventing a response."""

    request_path = Path(request_path).resolve()
    output_path = Path(output_path).resolve()
    request = _load_json_object(request_path, "manual review request")
    expected_fields = {
        "schema_version", "run_id", "effective_date", "mode",
        "prompt_version", "rules_sha256", "request_sha256", "item_count",
        "articles", "config_sha256", "no_manual_review_needed",
        "response_contract",
        "rss_fixture", "base_scores",
    }
    if (
        request.get("schema_version") not in {2, 3}
        or request.get("mode") != "interactive"
        or set(request) != expected_fields
    ):
        raise ValueError("unsupported no-manual interactive request")
    expected_sha = hashlib.sha256(
        _canonical_json(_request_identity(request)).encode("utf-8")
    ).hexdigest()
    if request.get("request_sha256") != expected_sha:
        raise ValueError("manual review request content hash mismatch")
    if (
        request.get("no_manual_review_needed") is not True
        or request.get("item_count") != 0
        or request.get("articles") != []
    ):
        raise ValueError("manual review is still required")
    fixture, rss_path, base_path = _compile_complete_fixture(
        request_path, request, {}, {}
    )
    _write_json_atomic(output_path, fixture)
    receipt = {
        "schema_version": 1,
        "component": "manual-review-import",
        "request_sha256": request["request_sha256"],
        "request_file_sha256": _sha256_file(request_path),
        "response_file_sha256": None,
        "rss_fixture_sha256": _sha256_file(rss_path),
        "base_scores_sha256": _sha256_file(base_path),
        "reviewer": "deterministic",
        "run_id": request.get("run_id"),
        "effective_date": request.get("effective_date"),
        "manual_item_count": 0,
        "total_item_count": len(fixture["scores"]),
        "no_manual_review_needed": True,
        "output": str(output_path),
        "output_sha256": _sha256_file(output_path),
        "validation": "passed",
    }
    receipt_path = output_path.with_suffix(output_path.suffix + ".receipt.json")
    _write_json_atomic(receipt_path, receipt)
    return fixture, receipt_path


def persist_completed_review_cache(request_path, response_path, output_path):
    """Publish already-imported scores only after their source run completed.

    Compilation and cache publication are deliberately separate transactions.
    A production resume may compile before running downstream stages, but its
    scores are reusable only after the terminal operator state is successful.
    """

    request_path = Path(request_path).resolve()
    response_path = Path(response_path).resolve()
    output_path = Path(output_path).resolve()
    receipt_path = output_path.with_suffix(output_path.suffix + ".receipt.json")
    promotion_path = output_path.with_suffix(
        output_path.suffix + ".cache-promotion.json"
    )
    request = _load_json_object(request_path, "manual review request")
    response = _load_json_object(response_path, "manual review response")
    output = _load_json_object(output_path, "compiled scored fixture")
    receipt = _load_json_object(receipt_path, "manual review import receipt")
    if request.get("schema_version") not in {2, 3}:
        raise ValueError("cache publication requires a complete interactive request")
    expected_request_sha = hashlib.sha256(
        _canonical_json(_request_identity(request)).encode("utf-8")
    ).hexdigest()
    if request.get("request_sha256") != expected_request_sha:
        raise ValueError("manual review request content hash mismatch")
    reviewer, article_by_id, score_by_id = _validate_manual_response(
        request, response
    )
    expected_output, _rss_path, _base_path = _compile_complete_fixture(
        request_path, request, article_by_id, score_by_id
    )
    if output != expected_output:
        raise ValueError("compiled scored fixture changed before cache publication")
    expected_receipt = {
        "request_sha256": request["request_sha256"],
        "request_file_sha256": _sha256_file(request_path),
        "response_file_sha256": _sha256_file(response_path),
        "reviewer": reviewer,
        "run_id": request.get("run_id"),
        "effective_date": request.get("effective_date"),
        "output": str(output_path),
        "output_sha256": _sha256_file(output_path),
        "validation": "passed",
    }
    if any(receipt.get(key) != value for key, value in expected_receipt.items()):
        raise ValueError("manual review import receipt changed before cache publication")

    run_dir = request_path.parent.parent
    production_state_path = run_dir / "gemini-production-state.json"
    invocation_state_path = run_dir / "folder-agent-operator-invocation.json"
    if production_state_path.is_file():
        state = _load_json_object(production_state_path, "production operator state")
        allowed_status = {"completed", "completed_degraded"}
        expected_component = "folder-agent-production-state"
    elif invocation_state_path.is_file():
        state = _load_json_object(invocation_state_path, "Folder-Agent operator state")
        allowed_status = {"completed"}
        expected_component = "folder-agent-full-flow-invocation"
    else:
        raise ValueError("cache publication requires a terminal Folder-Agent state")
    if (
        state.get("component") != expected_component
        or state.get("run_id") != request.get("run_id")
        or state.get("reviewer") != reviewer
        or state.get("status") not in allowed_status
    ):
        raise ValueError("source Folder-Agent run is not successfully completed")

    promotion = {
        "schema_version": 1,
        "component": "manual-review-cache-promotion",
        "validation": "passed",
        "run_id": request.get("run_id"),
        "effective_date": request.get("effective_date"),
        "reviewer": reviewer,
        "request_file_sha256": _sha256_file(request_path),
        "response_file_sha256": _sha256_file(response_path),
        "import_receipt_file_sha256": _sha256_file(receipt_path),
        "output_sha256": _sha256_file(output_path),
        "persisted_count": len(score_by_id),
    }
    if promotion_path.exists():
        if _load_json_object(promotion_path, "cache promotion receipt") != promotion:
            raise ValueError("a different cache promotion receipt already exists")
        return promotion_path
    _persist_verified_manual_scores(
        request,
        score_by_id,
        receipt,
        receipt_path,
        request_path,
        response_path,
        output_path,
    )
    _write_json_atomic(promotion_path, promotion)
    return promotion_path


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--no-persist-verified-cache", action="store_true")
    parser.add_argument("--persist-completed-cache-only", action="store_true")
    args = parser.parse_args(argv)
    if args.persist_completed_cache_only:
        receipt = persist_completed_review_cache(
            args.request,
            args.response,
            args.output,
        )
        print(f"MANUAL_REVIEW_CACHE_PROMOTION_RECEIPT={receipt}")
        return
    _, receipt = import_review(
        args.request,
        args.response,
        args.output,
        persist_verified_cache=not args.no_persist_verified_cache,
    )
    print(f"MANUAL_REVIEW_IMPORT_RECEIPT={receipt}")


if __name__ == "__main__":
    main()
