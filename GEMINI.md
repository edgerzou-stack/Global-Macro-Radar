# Gemini operator contract

## Mandatory bug regression protocol

Whenever the user reports an error, incorrect result, regression, or production
failure, do not change production code first. The required order is:

1. Add a focused regression test that reproduces the reported failure.
2. Run that test against the unmodified implementation and record evidence that
   it fails for the expected reason. A test that already passes does not prove
   the bug has been reproduced.
3. Apply the smallest in-scope implementation fix. Do not weaken, delete, skip,
   or rewrite an existing assertion merely to make the suite pass.
4. Run the focused regression test, the affected subsystem suite, the complete
   offline test suite, and `git diff --check`.
5. Report the test file and test name, the before-fix failure, the after-fix
   result, and the complete-suite result.

Every reported bug must receive a permanent regression case unless the user
explicitly approves a documented exception. If a reliable automated
reproduction is genuinely impossible, stop before changing production code and
ask the user to approve the exception. Test changes require integrity review:
they must assert externally meaningful behavior and must not be pseudo-tests
that only mirror implementation details or trivially pass.

When the user sends the exact instruction `跑全流程`, treat it as authorization
for exactly one invocation of the production entrypoint. Before the invocation,
print and confirm all three resolved identities:

1. project root: `/Users/zouzhengting/Workplace/Global-Macro-Radar`;
2. canonical database: `quant-strategy/quant_system.db`;
3. configured sender and recipient from `industry-radar/config.yaml`.

The sender/recipient confirmation must happen before any database write or SMTP
connection. Abort if the resolved database, sender, or recipient differs from
the values the user authorized.

The production entrypoint owns the auditable log and atomically truncates it
before each invocation. Run it directly; do not add a second outer `tee`
pipeline. The script itself uses `set -o pipefail` semantics and:

```bash
./scripts/run_production_full_flow.sh
```

That invocation authorizes one update of the canonical production database and
one real SMTP submission using the configured recipient. It does not authorize
code edits, threshold changes, database repair, fixture substitution, manual
price substitution, or a second invocation.

Operational rules:

1. Do not reinterpret the report timestamp as the trade timestamp. The report
   may run on any day and at any time.
2. Existing `PENDING` intents are immutable commitments. The executor looks up
   the raw open for each intent's original `eligible_session`, even if the next
   report is one or more days later. Missing authoritative data leaves the
   intent pending.
3. A closed market, pre-open market, or future eligible session does not block
   report generation or email; it is disclosed as pending.
   If the eligible session is the market-local current day but the official
   open has not occurred, classify it as `not_yet_due` and do not call the
   exact-open provider. Only a due session whose authoritative raw open is
   unavailable may be classified as deferred/degraded.
4. Never edit `eligible_session`, synthesize an opening price, use a current or
   previous close as an opening price, or cancel a pending intent because a
   later screen changed.
5. Never automatically retry the command or SMTP delivery. In particular,
   `sending` and `accepted_by_smtp` are non-retry states. Report their exact
   journal status and wait for the user.
   New mail subjects include the logical report date, and the sender blocks a
   second live submission to the same recipient and effective date across run
   IDs. Never use `--allow-duplicate-effective-date-delivery` without a separate,
   explicit resend authorization.
6. On failure, stop and report the failing phase, run ID, manifest path (if
   present), and delivery journal path (if present). Do not continue by changing
   code or data.
7. On success, report the run ID, completed manifest, production database
   SHA-256, report HTML path and SHA-256, delivery journal path, journal state,
   and configured recipient. `accepted_by_smtp` is not proof of inbox receipt.
   Never report generation or delivery success from a canonical report file
   alone. The completed manifest, prepared report, and delivery journal must all
   exist under the same current run directory and carry the same run ID. If the
   current run fails, do not display, attach, or cite a report from an older run.
8. Verify that `reports/latest_run_execution.log` exists, is non-empty, starts
   with exactly one run ID, contains the same run ID as the manifest and
   delivery journal, and ends in a terminal success or failure record. The
   standard log supplements the immutable per-run artifacts; it does not
   replace them.
   Also verify that the manifest contains `telemetry`, `stage_inputs`,
   `performance_baseline`, and `performance_evaluation`. A warming-up baseline
   is valid until the configured minimum number of comparable runs exists;
   never invent a performance conclusion from insufficient samples.
9. Classify the terminal outcome explicitly:
   - `completed`: the manifest and integrity gates pass and no configured
     degradation signal is present.
   - `completed_degraded`: the run completes, but any market still has pending
     or deferred intents, a provider circuit breaker or source failure occurred,
     a NAV used certified carry-forward, or decision/financial coverage is
     degraded. Report every applicable signal and never describe this state as
     fully healthy.
   - `failed`: any required phase, artifact verification, database integrity
     check, or SMTP submission gate fails.
10. If the recipient reports that a journal in `accepted_by_smtp` is not visible
    in the inbox, do not rerun the production command and do not resend:
    - ask the recipient to search the same-subject conversation, `Junk`, and
      mailbox rules by subject, sender, timestamp, and Message-ID;
    - with explicit mailbox-read authorization, inspect the sender's
      `Sent Messages` and bounce/undeliverable headers in read-only mode;
    - correlate by recipient, subject, and timestamp because the provider may
      rewrite the original Message-ID in its sent copy;
    - report the evidence and wait. A separately authorized new production run
      is required for any new SMTP submission.
    - only after the user explicitly confirms non-receipt and authorizes a new
      submission, invoke exactly
      `scripts/run_production_full_flow.sh --authorized-resend`. Never infer
      resend authorization from a generic request to rerun, debug, continue, or
      release. The flag permits a new run to bypass only the recipient/date
      dedupe gate; it does not bypass same-run idempotency, production write
      confirmation, report validation, or any other safety gate.
    - after the resend reaches `accepted_by_smtp`, stop and wait for the
      recipient to confirm actual receipt. Do not release until the accepted
      journal is reconciled to `confirmed_received`.
    - a separately authorized plain-text canary diagnoses the mail route only.
      Receiving a canary does not confirm receipt of the formal report and must
      never reconcile a production delivery journal. Formal delivery uses MIME
      schema v3 with the `icloud_safe_v1` profile: an ASCII subject, useful
      plain-text fallback, at most 1600-pixel-wide bounded CID PNGs, and a
      fail-closed 512 KiB serialized-message limit.
    - the production entrypoint runs the daily recipient/date delivery gate
      before starting any production work. A duplicate-gate failure means no
      new report was delivered; do not present an older mailbox copy as the
      result of the failed run.
11. Verify both rendered outputs from the same run-bound view:
    - Markdown and HTML contain all three strategy chapters;
    - a non-empty hotspot input produces visible news evidence in both outputs;
    - pending/filled/unified trade evidence has the same run identity in both;
    - a missing section is a failed run, not permission to resend or rerun.
12. Verify the trading-language contract:
    - the early NAV chart is backed by certified NAV snapshots;
    - no report contains `pnl_chart_all.png` or a sum of trade-level percentage
      returns presented as a portfolio return; only certified NAV may represent
      cumulative strategy performance;
    - “本次目标变化（尚未成交）” contains only `PENDING` intents whose
      `source_run_id` equals the current run;
    - older pending commitments remain visible only in the pending-intent
      table, and an unfilled intent never displays a fabricated entry/exit
      price or PnL.
13. Fixed-tranche additions are ledger actions, not report annotations. A
    retained one-tranche position may create one `ADD_TRANCHE` at a verified
    cumulative return of -10% or lower; a retained two-tranche position may
    create one at -15.5% or lower; three tranches is the hard cap. Missing
    return evidence fails closed. Settlement still requires the immutable
    eligible session's exact raw open and atomically updates cash, harmonic
    average cost, tranche count, snapshot, and v8 evidence.
14. A user's explicit confirmation that the formal report arrived closes the
    delivery loop; it does not authorize another production run or SMTP send.
    Reconcile only the accepted journal from the run the user inspected:

    ```bash
    .venv/bin/python quant-strategy/scripts/send_unified_email.py \
      --reconcile-confirmed-delivery \
      --confirm-recipient-received \
      --run-id RUN_ID \
      --artifact-dir reports/pipeline-runs/RUN_ID \
      --expected-html-sha256 VERIFIED_PREPARED_HTML_SHA256 \
      --expected-recipient VERIFIED_RECIPIENT
    ```

    Resolve every placeholder from that run's immutable artifacts; never copy
    values from an older run or infer receipt from SMTP acceptance, a canary,
    or the sender's Sent folder. Require the resulting journal to retain the
    same run ID, recipient, HTML hash and Message-ID, change only from
    `accepted_by_smtp` (or an explicitly reconciled ambiguous `sending` state)
    to `confirmed_received`, and keep `safe_to_retry=false`.
15. Before publishing a release, do not rerun the production full flow. Run
    `scripts/run_release_checks.sh` and `git diff --check`, then verify the
    reconciled `confirmed_received` journal, completed run manifest, prepared
    report hash, database integrity/FK/delete-protection/ledger gates, and the
    release diff. Exclude credentials, `.env`, databases, WAL files, runtime
    logs, pipeline-run artifacts, backups and caches from the commit. Publish
    only to the public remote through a reviewable branch/PR; `private/Core`
    remains read-only, and never merge PR #1 automatically.

For diagnosis or dry checks, use:

```bash
./scripts/run_production_full_flow.sh --preflight-only
```

The dry check does not update the database and does not send email. Do not run
the real command merely because the user asks to inspect or debug the system.
