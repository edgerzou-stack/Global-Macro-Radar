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
for exactly one invocation of:

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
4. Never edit `eligible_session`, synthesize an opening price, use a current or
   previous close as an opening price, or cancel a pending intent because a
   later screen changed.
5. Never automatically retry the command or SMTP delivery. In particular,
   `sending` and `accepted_by_smtp` are non-retry states. Report their exact
   journal status and wait for the user.
6. On failure, stop and report the failing phase, run ID, manifest path (if
   present), and delivery journal path (if present). Do not continue by changing
   code or data.
7. On success, report the run ID, completed manifest, production database
   SHA-256, report HTML path and SHA-256, delivery journal path, journal state,
   and configured recipient. `accepted_by_smtp` is not proof of inbox receipt.

For diagnosis or dry checks, use:

```bash
./scripts/run_production_full_flow.sh --preflight-only
```

The dry check does not update the database and does not send email. Do not run
the real command merely because the user asks to inspect or debug the system.
