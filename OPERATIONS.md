# Global Macro Radar operations

This is the operator runbook for the unified pipeline. Run commands from the
repository root. Paths shown as `/absolute/...` must be replaced with canonical
absolute paths. Never point a non-production mode at
`quant-strategy/quant_system.db`.

## Pipeline phases

`run_all.sh` invokes one identity-bound `daily_runner.py` process. A successful
run executes, in order:

1. stock API health check;
2. read-only database integrity check;
3. settle previously due v7 market intents at exact-session raw opens;
4. radar RSS ingest and news scoring;
5. universe refresh, hot-spot screen and global quantitative screen; this creates
   next-session intents but never claims immediate fills;
6. NAV calculation;
7. post-market ledger and intent sanity check;
8. PnL chart generation;
9. report generation and delivery.

The runner creates an SQLite Online Backup before changing an existing target
database, holds a database writer fence, and writes an identity-bound manifest.
A failed run keeps an identity-bound checkpoint for an explicit resume.

Settlement is market-specific and idempotent. Missing A/HK/US raw opens leave only
that intent pending. SELL intents run before BUY intents, failed exits retain their
holding slots, each strategy remains capped at ten actual positions, and A-share
same-session exits are rescheduled for T+1. HTML separates research candidates,
actual legacy positions and pending v7 intents.

The report date is a settlement cutoff, not a fill date. Each intent keeps the
`eligible_session` fixed when it is created. A run one, two, or seven days later
(including a weekend run) still requests that original session's unadjusted open.
If authoritative evidence is unavailable, the intent stays `PENDING`; a later
screen cannot cancel it, and pending buys count toward the ten-position capacity.

## Test and coverage gates

The default `pytest` discovery includes the quant, root, and Industry Radar
offline suites. Offline tests fail closed on socket access; a real external
probe must be explicitly marked `live` and is excluded from the default suite.
CI runs on Python 3.11 with branch coverage enabled and enforces a combined
50% floor across `quant-strategy/scripts` and `industry-radar`. Production
integrity and ledger `check_*.py` scripts are included in that denominator.

```bash
python -m pytest -q -p no:cacheprovider \
  -m "not live and not llm_eval and not slow" \
  --disable-socket --allow-unix-socket
```

The combined floor is a regression gate, not a reliability target. Add focused
branch tests and per-module gates for state-mutating or execution-critical code
instead of excluding low-coverage production modules.

## Run modes

| Mode | Database contract | Orders and delivery | Fixture contract |
| --- | --- | --- | --- |
| `offline` | isolated backtest database only | real orders disabled; delivery sink | `--fixture-root` required |
| `shadow` | isolated test database only | real orders disabled; delivery sink | optional |
| `live-shadow` | isolated test database only | real orders disabled; delivery sink | current date optional; historical date requires `--rss-fixture` |
| `production` | canonical `quant-strategy/quant_system.db` only | production fences apply; delivery defaults to sink | optional |

`live-shadow` has the same write and delivery safety profile as `shadow`. A
current-date run without fixtures uses the real RSS, market-data and configured
LLM adapters. A historical live-shadow must provide `--rss-fixture`; the fixture
path and SHA-256 are included in the run identity while market and financial
sources remain live. `PIPELINE_EFFECTIVE_DATE` binds persisted facts and
artifacts, while the timezone-aware run instant controls live-session checks.

### Offline fixture bundle

The fixture directory is a complete, fixed-name bundle. Missing files and
symlinks escaping the bundle root are rejected before any stage starts. File
paths and SHA-256 digests are included in the run configuration hash.

| Environment variable | Required file |
| --- | --- |
| `RADAR_RSS_FIXTURE` | `radar_rss.json` |
| `RADAR_SCORED_ARTICLES_FIXTURE` | `radar_scored_articles.json` |
| `HOT_SPOT_FIXTURE` | `hot_spot.json` |
| `UNIVERSE_FIXTURE` | `universe.json` |
| `GLOBAL_SCREEN_FIXTURE` | `global_screen.json` |
| `STOCK_API_HEALTH_FIXTURE` | `stock_api_health.json` |
| `HISTORICAL_PRICE_FIXTURE` | `historical_prices.json` |

Empty files are accepted only by the entry-contract tests. A real E2E run needs
valid JSON that satisfies each consuming stage's schema.

## Manual runs

Use an explicit run ID when a run may need to be resumed. Offline mode uses its
fixture effective date and does not query live trading calendars. `FORCE_RUN=1`
is only for a separately controlled shadow/live-shadow calendar bypass.

Offline, deterministic E2E:

```bash
./run_all.sh \
  --mode offline \
  --database /absolute/scratch/offline-e2e.db \
  --fixture-root /absolute/fixtures/e2e \
  --artifact-root /absolute/artifacts \
  --effective-date 2026-07-15 \
  --run-id offline-e2e-20260715
```

For a clean schema without audit anomalies, a read-consistent shadow seed may be
created without opening the source for writing:

```bash
sqlite3 quant-strategy/quant_system.db \
  ".backup '/absolute/scratch/shadow.db'"
```

The current legacy production database contains audit-selected anomalies and
must not use that direct seed for a full-flow acceptance. First run the release
coordinator in its default copy-only mode. It creates `working_copy.db`, applies
v6, v7 and additive quarantine only to the copy, and verifies legacy row counts:

```bash
python3 quant-strategy/scripts/production_release.py \
  --source-db "$(pwd)/quant-strategy/quant_system.db" \
  --audit /absolute/reports/production_db_audit.json \
  --output-dir /absolute/scratch/new-release-dir

SQLITE_DB_PATH=/absolute/scratch/new-release-dir/working_copy.db \
QUANT_DB_ENV=test PYTHONPATH=quant-strategy/scripts \
python3 -c 'import db_utils; connection=db_utils.init_db(); connection.close()'
```

Never add `--apply-production` or a confirmation token to this acceptance step.
Then run shadow or live-shadow with an explicit identity and sink delivery:

```bash
./run_all.sh \
  --mode shadow \
  --database /absolute/scratch/shadow.db \
  --artifact-root /absolute/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-shadow-run \
  --delivery-mode sink

./run_all.sh \
  --mode live-shadow \
  --database /absolute/scratch/new-release-dir/working_copy.db \
  --artifact-root /absolute/artifacts \
  --effective-date YYYY-MM-DD \
  --run-id unique-live-shadow-run \
  --delivery-mode sink
```

For a historical effective date, add an archived point-in-time RSS snapshot:

```bash
  --rss-fixture /absolute/fixtures/radar_rss_YYYY-MM-DD.json
```

Do not set unbound fixture environment variables manually. Success
requires a `completed` release manifest, a `completed` run manifest, a `sink`
delivery journal, `integrity_check=ok`, zero active non-positive entry prices,
and an unchanged production database hash. `shadow_runner.py --allow-live-api`
is a bounded source probe and does not replace this full flow.

NAV values each market at its latest officially completed session. An open
session is never treated as if its daily close already exists. If strict OHLC
validation fails only because of an unrelated open field, NAV performs a fresh
close-only read: close must remain positive, finite and bounded by high/low, and
the degraded row is not written into the OHLC cache.

Reporting is not gated on immediate settlement legality. A closed market,
pre-open run, or unavailable exact raw open leaves the affected intent
`PENDING` and records the per-market settlement outcome. NAV is certified per
strategy: a successful valuation writes a fresh snapshot; a market-data-only
failure may disclose the latest non-quarantined snapshot that satisfies
`nav = cash + holdings_value`; a strategy without such a snapshot is reported
as unavailable. The HTML validator checks these statuses and dates against the
run-bound database before delivery. Ledger-integrity failures remain blocking.

`run_all.sh` prefers the repository `.venv/bin/python` when present, and every
child stage inherits that same interpreter. Set `QUANT_PYTHON` or
`RADAR_PYTHON` only for an intentional override; production currently targets
Python 3.11 rather than the EOL macOS system Python 3.9.

Production requires both the explicit mode and a second write acknowledgement.
It also rejects every database path except the canonical production path:

```bash
./run_all.sh \
  --mode production \
  --database "$(pwd)/quant-strategy/quant_system.db" \
  --confirm-production-writes \
  --artifact-root "$(pwd)/quant-strategy/reports/pipeline-runs"
```

The first production run remains in `sink` mode. Inspect the generated
`delivery/<run-id>.html` and its journal before enabling SMTP. Live delivery
requires a separate, explicit acknowledgement and cannot be enabled in any
non-production mode:

```bash
./run_all.sh \
  --mode production \
  --database "$(pwd)/quant-strategy/quant_system.db" \
  --confirm-production-writes \
  --delivery-mode live \
  --confirm-live-delivery \
  --artifact-root "$(pwd)/quant-strategy/reports/pipeline-runs"
```

Setting `delivery.enabled: false` in `industry-radar/config.yaml` disables both
sink and live delivery. A live delivery journal left in `sending` state is
ambiguous and must be reconciled with the SMTP provider; never delete it to
force an automatic retry.

The sender loads the repository `.env` by default. Use `--env-file` or
`RADAR_ENV` only when the credential file lives elsewhere. The low-level sender
also enforces `--confirm-live-delivery`; invoking it directly cannot bypass the
second acknowledgement used by the unified runner.

To send an exact HTML artifact that has already been reviewed, calculate its
SHA-256 and give the delivery a new run ID. This path performs no database or
strategy work:

```bash
sha256sum /absolute/artifacts/reviewed-report.html

python3 quant-strategy/scripts/send_unified_email.py \
  --mode live \
  --confirm-live-delivery \
  --run-id unique-mail-canary \
  --artifact-dir /absolute/artifacts/mail-canary \
  --html-file /absolute/artifacts/reviewed-report.html \
  --expected-html-sha256 64_HEX_DIGEST
```

Live delivery converts base64 data images to related CID parts for broader mail
client compatibility and emits RFC `Date` plus a stable `Message-ID`. A
connection, TLS or login failure is recorded as `failed_pre_send`; an explicit
recipient refusal is `rejected_by_smtp`. A failure after the journal enters
`sending` remains ambiguous and must never be automatically retried. When
`SMTP.send_message()` returns no refused recipients, the journal becomes
`accepted_by_smtp` and records the SMTP endpoint, Message-ID, acceptance time,
recipient, HTML hash and inline image count. This proves outbound-server
acceptance only, not inbox delivery. After the recipient confirms receipt, use
the reconciliation command to record `confirmed_received`. Legacy `delivered`
journals remain terminal solely to prevent duplicates.

Before a production run, verify the database backup destination, delivery
recipients, API credentials, market date and that no release or other writer
holds the database fence. Do not set `FORCE_RUN=1` in routine production.

## Quarantine production release window

The database v6/quarantine release is separate from a normal daily production
run. The current authorization code permits mutation only on Saturday,
**2026-07-18 from 14:00 through 16:00 Asia/Shanghai**. It is not a recurring
every-Saturday window. Outside that exact interval the tool refuses production
mutation.

First stop all normal writers and generate a fresh audit. The audit must be no
more than 30 minutes old at apply time:

```bash
python3 quant-strategy/scripts/production_release.py \
  --refresh-audit-output /absolute/release/fresh-production-audit.json
```

Run the copy-only drill first. Apply production only after inspecting its
manifest and receiving the separate approval:

```bash
python3 quant-strategy/scripts/production_release.py \
  --audit /absolute/release/fresh-production-audit.json \
  --apply-production \
  --confirm-token APPLY-V6-QUARANTINE-2026-07-18 \
  --output-dir /absolute/release/production-20260718
```

See `quant-strategy/scripts/PRODUCTION_RELEASE.md` for validation and recovery
details. Never overwrite the live file during restore; restore into a new path
and request separate cutover approval.

## Scheduler

Scheduling is disabled by default. Merely starting Python, building an image, or
running `docker compose up` must not activate it.

A one-shot invocation does not enable the daemon:

```bash
python3 scheduler.py --run-now \
  --mode shadow \
  --database /absolute/scratch/shadow.db
```

The persistent loop requires explicit mode, database, times, and the additional
`--enable-scheduler` acknowledgement:

```bash
python3 scheduler.py \
  --enable-scheduler \
  --mode shadow \
  --database /absolute/scratch/shadow.db \
  --schedule-times 08:00,20:00
```

Times must use zero-padded 24-hour `HH:MM` syntax. Offline scheduling also
requires `--fixture-root`. Production scheduling additionally requires
`--confirm-production-writes` and the canonical production database. Live SMTP
also requires `--delivery-mode live --confirm-live-delivery`. Do not enable
production scheduling without an explicit operational approval.

Each scheduled pipeline has a two-hour default global timeout. Override it with
`--pipeline-timeout-seconds` only after measuring a representative shadow run.
On timeout the scheduler terminates the child, marks health as failed and does
not persist its shared JSON state. `SIGTERM`/`SIGINT` follow the same graceful
termination path, with a 30-second default grace period before `SIGKILL`.

## Docker deployment

The image runs as UID/GID `10001:10001`, with a read-only root filesystem. The
compose service is behind the `scheduler` profile and the daemon separately
requires `SCHEDULER_ENABLED=YES`. These are two independent opt-ins.
The Compose restart policy also defaults to `no`, so an incomplete configuration
fails once instead of entering a restart loop. Set
`SCHEDULER_RESTART_POLICY=unless-stopped` only for an explicitly configured
persistent scheduler.

SQLite uses WAL mode, and JSON stages use atomic temporary-file replacement.
Therefore `/data` is a writable **directory mount**, not a single-file database
mount. The image's canonical database path is a symlink to
`/data/quant-strategy/quant_system.db`, preserving the production-path guard.
The application tree is copied from the immutable image into a per-container
tmpfs at `/app/quant-strategy`. This gives stages a writable common directory
without making the image root writable. Reports, caches and logs are nested
persistent mounts. Successful runs atomically persist the four shared JSON state
files under `/data/quant-strategy/state/<mode>` and restore only the matching
mode before its next run.

Before first start, create every bind source. On a dedicated Linux deployment
host, make writable mounts accessible to UID/GID 10001. Docker Desktop may map
host ownership differently, but the paths must still be writable from the
container.

```bash
mkdir -p runtime/data/quant-strategy/cache \
  runtime/data/quant-strategy/scripts-cache \
  runtime/data/quant-strategy/reports runtime/data/quant-strategy/logs \
  runtime/data/quant-strategy/state/production \
  runtime/data/quant-strategy/state/shadow runtime/data/industry-radar \
  fixtures logs industry-radar/reports

sqlite3 quant-strategy/quant_system.db \
  ".backup 'runtime/data/quant-strategy/quant_system.db'"
sqlite3 quant-strategy/quant_system.db \
  ".backup 'runtime/data/quant-strategy/shadow.db'"

cp -p quant-strategy/global_screen.json \
  runtime/data/quant-strategy/state/production/global_screen.json
cp -p quant-strategy/hot_spot_today.json \
  runtime/data/quant-strategy/state/production/hot_spot_today.json
cp -p quant-strategy/universes.json \
  runtime/data/quant-strategy/state/production/universes.json
cp -p runtime/data/quant-strategy/state/production/*.json \
  runtime/data/quant-strategy/state/shadow/
cp -p industry-radar/article_cache.json runtime/data/industry-radar/article_cache.json

sudo chown -R 10001:10001 runtime/data logs industry-radar/reports
chmod -R u+rwX runtime/data logs industry-radar/reports
chmod -R a+rX fixtures
```

The SQLite `.backup` commands are read-consistent; do not replace them with a
raw file copy while another writer may be active. Initial JSON state is optional
for a fresh environment. If seeded, keep it under the intended mode directory
so offline, shadow and production runs cannot reuse one another's state.

`industry-radar/config.yaml` must already exist and is mounted read-only. Never
put credentials in the image; supply them through the untracked root `.env` or
the deployment secret store.

Build and perform a one-shot offline run:

```bash
docker compose --profile scheduler build radar-app

docker compose --profile scheduler run --rm radar-app \
  --run-now \
  --mode offline \
  --database /data/quant-strategy/offline-e2e.db \
  --fixture-root /fixtures \
  --artifact-root /app/quant-strategy/reports/pipeline-runs
```

Enable a persistent shadow schedule only with all explicit settings:

```bash
PIPELINE_MODE=shadow \
PIPELINE_DATABASE=/data/quant-strategy/shadow.db \
SCHEDULER_TIMES=08:00,20:00 \
SCHEDULER_ENABLED=YES \
SCHEDULER_RESTART_POLICY=unless-stopped \
docker compose --profile scheduler up -d radar-app
```

For offline Docker scheduling also set `PIPELINE_FIXTURE_ROOT=/fixtures` and
`FIXTURE_HOST_DIR=/absolute/host/fixtures`. For a separately approved production
schedule, set `PIPELINE_DATABASE=/data/quant-strategy/quant_system.db` and
`CONFIRM_PRODUCTION_WRITES=YES`; never reuse the default
`/data/quant-strategy/shadow.db` as a production path.
To enable live SMTP in that separately approved production schedule, also set
`DELIVERY_MODE=live` and `CONFIRM_LIVE_DELIVERY=YES`. The safer default remains
`DELIVERY_MODE=sink`.

Inspect status and stop the scheduler with:

```bash
docker compose --profile scheduler ps
docker compose --profile scheduler logs --tail=200 radar-app
docker compose --profile scheduler down
```

The scheduler refreshes its health heartbeat while a long pipeline is running,
so a normal multi-minute run does not create a false unhealthy status.

## Failure rules

- Any stage failure stops subsequent stages and returns a non-zero status.
- A missing or malformed fixture must not fall back to a live source in offline
  mode.
- A non-production run must never target the canonical production database.
- A production run without the second confirmation must not start.
- A scheduler without its explicit enable switch must exit instead of waiting.
- Writer-lock contention must be investigated; do not delete lock files to force
  concurrent writers.
- Quarantined anomalous data is isolated, not guessed or silently repaired.
