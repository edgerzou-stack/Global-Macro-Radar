#!/usr/bin/env python3
"""Verify hashes, exact paths and Python syntax in a public export candidate."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

from check_public_release_boundary import BoundaryPolicyError, audit_root, load_policy


MANIFEST_NAME = ".public-export-manifest.json"
MANIFEST_SCHEMA_VERSION = 1


class PublicExportVerificationError(RuntimeError):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def candidate_fingerprint(entries):
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item["destination"]):
        digest.update(entry["destination"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_manifest(root):
    path = root / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicExportVerificationError(f"cannot load {MANIFEST_NAME}: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise PublicExportVerificationError("unsupported public export manifest schema")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise PublicExportVerificationError("manifest entries must be a list")
    return payload


def verify_export(root, policy_path):
    root = root.resolve()
    policy = load_policy(policy_path)
    manifest = load_manifest(root)
    if manifest.get("policy_version") != policy["policy_version"]:
        raise PublicExportVerificationError("manifest/policy version mismatch")
    expected = {entry["destination"] for entry in policy["entries"]}
    records = manifest["entries"]
    destinations = [entry.get("destination") for entry in records if isinstance(entry, dict)]
    if len(destinations) != len(set(destinations)):
        raise PublicExportVerificationError("manifest contains duplicate destinations")
    if set(destinations) != expected:
        raise PublicExportVerificationError("manifest destination set differs from policy")
    verified = []
    for record in records:
        destination = record["destination"]
        candidate = root / destination
        if candidate.is_symlink() or not candidate.is_file():
            raise PublicExportVerificationError(f"missing regular file: {destination}")
        actual_hash = sha256_file(candidate)
        if actual_hash != record.get("sha256"):
            raise PublicExportVerificationError(f"hash mismatch: {destination}")
        if candidate.stat().st_size != record.get("size"):
            raise PublicExportVerificationError(f"size mismatch: {destination}")
        verified.append({"destination": destination, "sha256": actual_hash})
    fingerprint = candidate_fingerprint(verified)
    if fingerprint != manifest.get("candidate_fingerprint"):
        raise PublicExportVerificationError("candidate fingerprint mismatch")
    audit = audit_root(root, policy)
    if not audit["ok"]:
        first = audit["violations"][0]
        raise PublicExportVerificationError(
            f"boundary violation: {first['path']} ({first['reason']})"
        )
    python_files = sorted(root.rglob("*.py"))
    for python_file in python_files:
        try:
            ast.parse(python_file.read_text(encoding="utf-8"), filename=str(python_file))
        except (OSError, SyntaxError, UnicodeError) as error:
            raise PublicExportVerificationError(
                f"Python syntax verification failed for {python_file.relative_to(root)}: {error}"
            ) from error
    return {
        "status": "verified",
        "policy_version": policy["policy_version"],
        "file_count": audit["accepted_count"],
        "python_file_count": len(python_files),
        "candidate_fingerprint": fingerprint,
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Public candidate root")
    parser.add_argument(
        "--policy",
        default="PUBLIC_EXPORT_POLICY.json",
        help="Exported exact policy relative to candidate root",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    policy_path = Path(args.policy)
    if not policy_path.is_absolute():
        policy_path = root / policy_path
    try:
        result = verify_export(root, policy_path)
    except (BoundaryPolicyError, OSError, PublicExportVerificationError) as error:
        print(f"public export verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
