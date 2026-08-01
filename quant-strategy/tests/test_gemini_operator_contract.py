from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = PROJECT_ROOT / "GEMINI.md"
PRODUCTION_ENTRYPOINT = PROJECT_ROOT / "scripts" / "run_production_full_flow.sh"
RELEASE_CHECK_ENTRYPOINT = PROJECT_ROOT / "scripts" / "run_release_checks.sh"
RELEASE_COVERAGE_CHECKER = PROJECT_ROOT / "scripts" / "check_release_coverage.py"


def _contract_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def test_full_flow_contract_requires_auditable_logged_invocation():
    contract = _contract_text()

    assert "reports/latest_run_execution.log" in contract
    assert "pipefail" in contract
    assert "tee" in contract
    assert "non-empty" in contract or "非空" in contract
    assert "run ID" in contract


def test_full_flow_contract_confirms_database_and_delivery_identity_before_send():
    contract = _contract_text()

    assert "quant-strategy/quant_system.db" in contract
    assert "sender" in contract.lower() or "发件人" in contract
    assert "recipient" in contract.lower() or "收件人" in contract
    assert "before" in contract.lower() or "之前" in contract


def test_full_flow_contract_classifies_degraded_success():
    contract = _contract_text()

    assert "completed_degraded" in contract
    assert "carry-forward" in contract
    assert "pending" in contract.lower()
    assert "coverage" in contract.lower()


def test_full_flow_contract_diagnoses_missing_mail_without_resending():
    contract = _contract_text()

    assert "accepted_by_smtp" in contract
    assert "Sent Messages" in contract
    assert "Junk" in contract
    assert "Message-ID" in contract
    assert "conversation" in contract.lower()
    assert "do not rerun" in contract.lower()
    assert "scripts/run_production_full_flow.sh --authorized-resend" in contract
    assert "confirmed_received" in contract
    assert "plain-text canary" in contract
    assert "icloud_safe_v1" in contract
    assert "512 KiB" in contract


def test_full_flow_contract_never_substitutes_an_old_report_for_a_failed_run():
    contract = _contract_text()

    assert "same current run directory" in contract
    assert "do not display, attach, or cite a report from an older run" in contract
    assert "before starting any production work" in contract


def test_receipt_confirmation_reconciles_the_exact_accepted_run_without_resend():
    contract = _contract_text()

    assert "--reconcile-confirmed-delivery" in contract
    assert "--confirm-recipient-received" in contract
    assert "--expected-html-sha256" in contract
    assert "--expected-recipient" in contract
    assert "same run ID, recipient, HTML hash and Message-ID" in contract
    assert "confirmed_received" in contract
    assert "safe_to_retry=false" in contract
    assert "does not authorize another production run or SMTP send" in contract


def test_release_contract_is_public_only_and_excludes_runtime_artifacts():
    contract = _contract_text()

    assert "scripts/run_release_checks.sh" in contract
    assert "git diff --check" in contract
    assert "pipeline-run artifacts" in contract
    assert "public remote" in contract
    assert "private/Core" in contract
    assert "never merge PR #1 automatically" in contract


def test_production_entrypoint_remains_single_run_boundary():
    script = PRODUCTION_ENTRYPOINT.read_text(encoding="utf-8")

    assert script.count('"$ROOT_DIR/run_all.sh"') == 1
    assert script.count("--check-daily-delivery-gate") == 1
    assert script.index("--check-daily-delivery-gate") < script.index(
        '"$ROOT_DIR/run_all.sh"'
    )
    assert "--confirm-production-writes" in script
    assert "--confirm-live-delivery" in script
    assert "--authorized-resend" in script
    assert "--allow-duplicate-effective-date-delivery" in script


def test_production_entrypoint_captures_latest_log_without_outer_wrapper():
    script = PRODUCTION_ENTRYPOINT.read_text(encoding="utf-8")

    assert 'LOG_FILE="$ROOT_DIR/reports/latest_run_execution.log"' in script
    assert 'exec > >(tee "$LOG_FILE") 2>&1' in script
    assert 'if [ "$PREFLIGHT_ONLY" -ne 1 ]; then' in script


def test_release_coverage_gate_is_executable_and_branch_aware():
    assert RELEASE_CHECK_ENTRYPOINT.is_file()
    assert RELEASE_COVERAGE_CHECKER.is_file()

    script = RELEASE_CHECK_ENTRYPOINT.read_text(encoding="utf-8")
    checker = RELEASE_COVERAGE_CHECKER.read_text(encoding="utf-8")

    assert "--cov-branch" in script
    assert "--cov-report=json:" in script
    assert "check_release_coverage.py" in script
    assert "GLOBAL_BRANCH_FLOOR = 23.0" in checker
    assert "CRITICAL_MODULE_BRANCH_FLOORS" in checker
