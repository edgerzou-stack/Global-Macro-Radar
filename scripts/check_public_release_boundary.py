#!/usr/bin/env python3
"""Fail closed when a proposed public tree exceeds its exact release policy."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


POLICY_SCHEMA_VERSION = 1
DEFAULT_SOURCE_POLICY = Path("public/public_export_policy.json")
DEFAULT_EXPORTED_POLICY = Path("PUBLIC_EXPORT_POLICY.json")
PRIVATE_COMPONENTS = frozenset(
    {
        ".agents",
        ".ai-context",
        ".ai-memory",
        ".reasoning",
        "__pycache__",
        "artifacts",
        "backups",
        "fixtures",
        "logs",
        "reports",
        "scratch",
        "test",
        "tests",
    }
)
PRIVATE_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".sqlite",
    ".sqlite3",
)
PRIVATE_EXACT_PATHS = frozenset(
    {
        ".env",
        "GEMINI.md",
        "OPERATIONS.md",
        "com.globalmacroradar.plist",
        "docker-compose.yml",
        "industry-radar/.env",
        "industry-radar/article_cache.json",
        "industry-radar/config.yaml",
        "industry-radar/md_testset.json",
        "run_all.sh",
        "scheduler.py",
        "scripts/build_public_export.py",
        "scripts/run_production_full_flow.sh",
        "scripts/run_release_checks.sh",
    }
)
SECRET_PATTERNS = (
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("github_fine_grained_token", re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("google_api_key", re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("openai_style_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
)


class BoundaryPolicyError(ValueError):
    """The public release policy itself is malformed or unsafe."""


def normalize_path(value: str) -> str:
    raw = str(value).strip().replace("\\", "/")
    if not raw:
        return ""
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise BoundaryPolicyError(f"unsafe relative path: {value!r}")
    normalized = str(path)
    return "" if normalized == "." else normalized.removeprefix("./")


def _validated_string_list(payload, name):
    values = payload.get(name, [])
    if not isinstance(values, list):
        raise BoundaryPolicyError(f"{name} must be a list")
    normalized = []
    for value in values:
        if not isinstance(value, str):
            raise BoundaryPolicyError(f"{name} entries must be strings")
        normalized.append(normalize_path(value))
    if len(set(normalized)) != len(normalized):
        raise BoundaryPolicyError(f"{name} contains duplicate paths")
    return normalized


def load_policy(policy_path) -> dict:
    path = Path(policy_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BoundaryPolicyError(f"cannot load public policy {path}: {error}") from error
    if not isinstance(payload, dict):
        raise BoundaryPolicyError("public policy must be an object")
    if payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise BoundaryPolicyError(
            f"unsupported public policy schema: {payload.get('schema_version')!r}"
        )
    policy_version = payload.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise BoundaryPolicyError("policy_version must be a non-empty string")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise BoundaryPolicyError("entries must be a non-empty list")
    normalized_entries = []
    sources = set()
    destinations = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BoundaryPolicyError(f"entries[{index}] must be an object")
        source = normalize_path(entry.get("source", ""))
        destination = normalize_path(entry.get("destination", ""))
        if not source or not destination:
            raise BoundaryPolicyError(f"entries[{index}] paths must be non-empty")
        if source in sources:
            raise BoundaryPolicyError(f"duplicate source path: {source}")
        if destination in destinations:
            raise BoundaryPolicyError(f"duplicate destination path: {destination}")
        sources.add(source)
        destinations.add(destination)
        normalized_entries.append({"source": source, "destination": destination})
    generated_paths = _validated_string_list(payload, "generated_paths")
    overlap = destinations.intersection(generated_paths)
    if overlap:
        raise BoundaryPolicyError(
            f"generated paths overlap copied destinations: {sorted(overlap)}"
        )
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_version": policy_version.strip(),
        "entries": normalized_entries,
        "generated_paths": generated_paths,
        "allowed_destinations": destinations.union(generated_paths),
    }


def classify_public_path(value: str, allowed_paths=None) -> tuple[bool, str]:
    try:
        path = normalize_path(value)
    except BoundaryPolicyError:
        return False, "unsafe_path"
    if not path:
        return False, "empty_path"
    parts = PurePosixPath(path).parts
    if path in PRIVATE_EXACT_PATHS:
        return False, "private_exact_path"
    if any(component in PRIVATE_COMPONENTS for component in parts):
        return False, "private_path_component"
    if path.endswith(PRIVATE_SUFFIXES):
        return False, "private_file_suffix"
    if any(part.startswith("test_") for part in parts):
        return False, "private_test_file"
    if path.startswith("quant-strategy/"):
        return False, "private_quant_strategy"
    if allowed_paths is None or path not in allowed_paths:
        return False, "not_on_exact_manifest"
    return True, "exact_manifest"


def audit_paths(paths, allowed_paths=None) -> dict:
    violations = []
    accepted = 0
    for raw_path in paths:
        try:
            path = normalize_path(raw_path)
        except BoundaryPolicyError:
            path = str(raw_path)
        if not path:
            continue
        allowed, reason = classify_public_path(path, allowed_paths)
        if allowed:
            accepted += 1
        else:
            violations.append({"path": path, "reason": reason})
    return {
        "accepted_count": accepted,
        "violation_count": len(violations),
        "violations": violations,
        "ok": not violations,
    }


def tracked_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [item for item in result.stdout.decode("utf-8").split("\0") if item]


def scan_root(root) -> tuple[list[str], list[dict]]:
    base = Path(root).resolve()
    if not base.is_dir():
        raise BoundaryPolicyError(f"public candidate root is not a directory: {base}")
    paths = []
    violations = []
    for current, directory_names, file_names in os.walk(base, followlinks=False):
        current_path = Path(current)
        retained_directories = []
        for name in sorted(directory_names):
            candidate = current_path / name
            relative = candidate.relative_to(base).as_posix()
            if relative == ".git":
                continue
            if candidate.is_symlink():
                violations.append({"path": relative, "reason": "symlink"})
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in sorted(file_names):
            candidate = current_path / name
            relative = candidate.relative_to(base).as_posix()
            if candidate.is_symlink():
                violations.append({"path": relative, "reason": "symlink"})
            elif not candidate.is_file():
                violations.append({"path": relative, "reason": "not_regular_file"})
            else:
                paths.append(relative)
    return paths, violations


def scan_secrets(root, paths) -> list[dict]:
    base = Path(root).resolve()
    findings = []
    for relative in paths:
        candidate = base / relative
        try:
            data = candidate.read_bytes()
        except OSError as error:
            findings.append(
                {"path": relative, "reason": "unreadable_file", "detail": str(error)}
            )
            continue
        if b"\0" in data:
            findings.append({"path": relative, "reason": "binary_file"})
            continue
        if relative == "config/manual_review_cache_revocations.json":
            try:
                public_policy = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                public_policy = None
            if public_policy != {"schema_version": 1, "incidents": []}:
                findings.append({"path": relative, "reason": "private_manual_review_policy"})
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(data):
                findings.append(
                    {"path": relative, "reason": "secret_pattern", "detail": name}
                )
    return findings


def audit_root(root, policy) -> dict:
    paths, filesystem_violations = scan_root(root)
    result = audit_paths(paths, policy["allowed_destinations"])
    secret_violations = scan_secrets(root, paths)
    result["violations"].extend(filesystem_violations)
    result["violations"].extend(secret_violations)
    result["violation_count"] = len(result["violations"])
    result["filesystem_violation_count"] = len(filesystem_violations)
    result["secret_violation_count"] = len(secret_violations)
    result["ok"] = not result["violations"]
    return result


def default_policy_path(root=None) -> Path:
    if root:
        exported = Path(root) / DEFAULT_EXPORTED_POLICY
        if exported.is_file():
            return exported
    if DEFAULT_SOURCE_POLICY.is_file():
        return DEFAULT_SOURCE_POLICY
    return DEFAULT_EXPORTED_POLICY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a public release tree against an exact fail-closed manifest"
    )
    parser.add_argument("--policy", help="Exact public export policy JSON")
    parser.add_argument("--root", help="Candidate root to scan, including untracked files")
    parser.add_argument(
        "--paths-file",
        help="Optional newline-delimited destination manifest; defaults to git ls-files",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON evidence")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy_path = Path(args.policy) if args.policy else default_policy_path(args.root)
        policy = load_policy(policy_path)
        if args.root:
            result = audit_root(args.root, policy)
        else:
            if args.paths_file:
                with open(args.paths_file, encoding="utf-8") as handle:
                    paths = [line.strip() for line in handle if line.strip()]
            else:
                paths = tracked_paths()
            result = audit_paths(paths, policy["allowed_destinations"])
        result["policy_version"] = policy["policy_version"]
    except (BoundaryPolicyError, OSError, subprocess.SubprocessError) as error:
        result = {
            "ok": False,
            "policy_version": "unavailable",
            "accepted_count": 0,
            "violation_count": 1,
            "violations": [{"path": "<policy>", "reason": "policy_error", "detail": str(error)}],
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    elif result["ok"]:
        print(
            "Public release boundary OK: "
            f"{result['accepted_count']} exact-manifest paths "
            f"({result['policy_version']})"
        )
    else:
        print(
            "Public release boundary FAILED: "
            f"{result['violation_count']} private, unexpected or unsafe paths",
            file=sys.stderr,
        )
        for violation in result["violations"][:50]:
            detail = f" ({violation['detail']})" if violation.get("detail") else ""
            print(
                f"- {violation['path']}: {violation['reason']}{detail}",
                file=sys.stderr,
            )
        if result["violation_count"] > 50:
            print("- additional violations omitted", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
