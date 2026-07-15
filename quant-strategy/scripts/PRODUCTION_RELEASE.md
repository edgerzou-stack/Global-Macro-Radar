# Production v6/quarantine release

`production_release.py` is fail-closed. Its default mode never opens the source
database for writing. It creates two SQLite Online Backup copies, validates the
audit fingerprint and v6 schemas, applies v6 twice on the working copy, copies
the exact audit candidates into additive quarantine evidence tables, and writes
a release manifest plus restore instructions.

## Dry run

From `quant-strategy`:

```bash
python3 scripts/production_release.py \
  --source-db /absolute/path/to/quant_system.db \
  --audit /absolute/path/to/production_db_audit_20260715.json \
  --output-dir /absolute/path/to/new-release-directory
```

The output directory must not already exist. Inspect:

- `pre_release_backup.db`: immutable pre-release snapshot.
- `working_copy.db`: migrated/quarantine drill database.
- `post_release_copy.db`: verified post-drill backup.
- `release_manifest.json`: checks, fingerprints and candidate counts.
- `RESTORE_INSTRUCTIONS.md`: non-destructive recovery procedure.

The quarantine tables contain exact JSON copies only. Legacy rows are never
updated or deleted, and NULL/zero prices are not inferred.

## Production mode

Production mode is accepted only when all conditions hold:

First stop normal writers and generate a same-window fresh audit. This preserves
the original candidate selectors byte-for-byte but recomputes the source hash,
WAL hash, schema version, table counts, integrity checks and candidate matches:

```bash
python3 scripts/production_release.py \
  --refresh-audit-output /absolute/path/to/fresh_production_audit.json
```

Refresh fails if candidate counts/duplicate structure changed, candidate
selectors changed, or a new unpriced/test/corruption/negative/invalid-JSON row
is not covered by an approved selector. It never skips or weakens the source
hash check. Production release requires this fresh audit to be at most 30
minutes old.

Production mode is accepted only when all conditions hold:

1. `--source-db` resolves to the canonical `quant-strategy/quant_system.db`.
2. `--apply-production` is present.
3. `--confirm-token APPLY-V6-QUARANTINE-2026-07-18` is present.
4. Current time is within 2026-07-18 14:00–16:00 Asia/Shanghai.
5. The supplied fresh audit is no more than 30 minutes old.
6. Source SHA-256, WAL SHA-256, schema version and fresh table counts still
   match the fresh audit JSON.
7. Integrity, foreign-key, schema-collision and full copy-drill checks pass.

Example syntax, to be run only in the approved maintenance window:

```bash
python3 scripts/production_release.py \
  --audit /absolute/path/to/fresh_production_audit.json \
  --apply-production \
  --confirm-token APPLY-V6-QUARANTINE-2026-07-18 \
  --output-dir /absolute/path/to/new-production-release-directory
```

The command creates a verified pre-release backup before mutation and a verified
post-release backup afterward. Any failure stops subsequent steps and records
the error in the release manifest when the release directory has already been
created. Never overwrite the live DB during restore; restore to a new path and
request a separate cutover approval.
