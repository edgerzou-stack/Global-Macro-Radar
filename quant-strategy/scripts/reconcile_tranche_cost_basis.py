#!/usr/bin/env python3
"""Repair evidence-backed legacy ADD_TRANCHE cost-basis omissions.

The command is read-only unless all production acknowledgements are present.
It never changes shares, cash, intents, evidence, history, or quarantine rows.
Before touching production it rehearses the exact UPDATE set on two SQLite
online backups and runs the full read-only ledger sanity check on the rehearsal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from check_ledger_sanity import check_ledger
from core.quarantine import quarantine_filter
from core.writer_lock import writer_fence
from db_utils import get_production_db_path


CONFIRM_TOKEN = "RECONCILE-TRANCHE-COST-BASIS-V8"


class ReconciliationError(RuntimeError):
    pass


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _online_backup(source, destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(destination)
    with sqlite3.connect(str(source), timeout=30.0) as source_connection:
        with sqlite3.connect(str(destination), timeout=30.0) as target_connection:
            source_connection.backup(target_connection)
    with sqlite3.connect(str(destination), timeout=30.0) as verification:
        if verification.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ReconciliationError(f"Backup integrity check failed: {destination}")
        if verification.execute("PRAGMA foreign_key_check").fetchall():
            raise ReconciliationError(f"Backup foreign-key check failed: {destination}")
    return destination


def _validated_fill(row):
    (
        intent_id,
        action,
        execution_price,
        quantity,
        session,
        payload_sha256,
        payload_json,
    ) = row
    if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != payload_sha256:
        raise ReconciliationError(f"Execution evidence hash mismatch: {intent_id}")
    try:
        payload = json.loads(payload_json)
        payload_price = float(payload["open"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ReconciliationError(
            f"Execution evidence has no valid raw open: {intent_id}"
        ) from error
    price = float(execution_price)
    quantity = int(quantity)
    payload_price_mismatch = abs(payload_price - price) > 1e-9 * max(
        1.0, abs(price)
    )
    if (
        not math.isfinite(price)
        or price <= 0
        or quantity <= 0
        or payload.get("session") != session
        or payload.get("price_field") != "open"
        or payload.get("adjustment") != "raw"
        or (payload_price_mismatch and action != "BUY_NEW")
    ):
        raise ReconciliationError(f"Execution evidence does not match intent: {intent_id}")
    return action, price, quantity, payload_price_mismatch


def build_plan(connection, effective_date):
    cutoff = date.fromisoformat(str(effective_date)).isoformat()
    connection.row_factory = sqlite3.Row
    portfolio_filter, portfolio_parameters, _ = quarantine_filter(
        connection, "portfolio", table_alias="p"
    )
    enforced = {
        row[0].split(":", 1)[1]
        for row in connection.execute(
            "SELECT key FROM meta_data WHERE key LIKE 'cash_replay_enforced:%'"
        )
    }
    positions = connection.execute(
        "SELECT p.id,p.strategy,p.name_or_code,p.entry_date,p.entry_price,p.shares "
        "FROM portfolio p WHERE 1=1" + portfolio_filter,
        portfolio_parameters,
    ).fetchall()
    plan = []
    for position in positions:
        strategy = position["strategy"]
        symbol = position["name_or_code"]
        if strategy not in enforced:
            continue
        fills = connection.execute(
            """
            SELECT i.intent_id,i.action,i.execution_price,i.tranche_quantity,
                   i.eligible_session,e.payload_sha256,e.payload_json
            FROM trade_intents i
            JOIN trade_execution_evidence e ON e.intent_id=i.intent_id
            WHERE i.strategy_id=? AND i.symbol=? AND i.state='FILLED'
              AND i.action IN ('BUY_NEW','ADD_TRANCHE')
              AND i.eligible_session>=? AND i.eligible_session<=?
              AND NOT EXISTS (
                  SELECT 1 FROM trade_intent_supersessions s
                  WHERE s.intent_id=i.intent_id
              )
            ORDER BY i.executed_at,i.created_at,i.intent_id
            """,
            (strategy, symbol, position["entry_date"], cutoff),
        ).fetchall()
        if not fills:
            raise ReconciliationError(
                f"Replay-enforced position lacks filled purchase intents: {strategy}/{symbol}"
            )
        if sum(row[1] == "BUY_NEW" for row in fills) != 1:
            raise ReconciliationError(
                f"Ambiguous purchase lifecycle: {strategy}/{symbol}"
            )
        total_quantity = 0
        reciprocal_cost = 0.0
        add_count = 0
        evidence_ids = []
        legacy_buy_evidence_mismatches = []
        for fill in fills:
            payload = json.loads(fill[6])
            if payload.get("symbol") != symbol:
                raise ReconciliationError(
                    f"Execution evidence symbol mismatch: {fill[0]}"
                )
            action, price, quantity, payload_price_mismatch = _validated_fill(fill)
            total_quantity += quantity
            reciprocal_cost += quantity / price
            add_count += action == "ADD_TRANCHE"
            evidence_ids.append(fill[0])
            if payload_price_mismatch:
                legacy_buy_evidence_mismatches.append(fill[0])
        if int(position["shares"] or 0) != total_quantity:
            raise ReconciliationError(
                f"Share count cannot be repaired safely: {strategy}/{symbol}; "
                f"ledger={position['shares']} evidence={total_quantity}"
            )
        expected = total_quantity / reciprocal_cost
        actual = float(position["entry_price"])
        if abs(actual - expected) < 0.01:
            continue
        if add_count == 0:
            raise ReconciliationError(
                f"Non-tranche cost mismatch cannot be repaired safely: {strategy}/{symbol}"
            )
        plan.append(
            {
                "portfolio_id": int(position["id"]),
                "strategy": strategy,
                "symbol": symbol,
                "entry_date": position["entry_date"],
                "shares": total_quantity,
                "entry_price_before": actual,
                "entry_price_after": expected,
                "filled_purchase_intents": evidence_ids,
                "legacy_buy_evidence_mismatches": legacy_buy_evidence_mismatches,
            }
        )
    return plan


def apply_plan(connection, plan):
    for item in plan:
        result = connection.execute(
            "UPDATE portfolio SET entry_price=? "
            "WHERE id=? AND strategy=? AND name_or_code=? AND entry_date=? "
            "AND shares=? AND entry_price=?",
            (
                item["entry_price_after"],
                item["portfolio_id"],
                item["strategy"],
                item["symbol"],
                item["entry_date"],
                item["shares"],
                item["entry_price_before"],
            ),
        )
        if result.rowcount != 1:
            raise ReconciliationError(
                f"Portfolio row drifted before repair: {item['strategy']}/{item['symbol']}"
            )


def reconcile_production(
    database,
    *,
    effective_date,
    expected_sha256,
    output_dir,
    confirm_token,
    apply_production,
):
    database = Path(database).expanduser().resolve()
    canonical = Path(get_production_db_path()).expanduser().resolve()
    if not apply_production:
        raise ReconciliationError("Production repair requires --apply-production")
    if confirm_token != CONFIRM_TOKEN:
        raise ReconciliationError("Production repair confirmation token is incorrect")
    if database != canonical:
        raise ReconciliationError("Production repair requires the canonical database")
    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(output_dir)

    with writer_fence(database, owner="reconcile_tranche_cost_basis", timeout=0.0):
        actual_sha = _sha256_file(database)
        if actual_sha != expected_sha256:
            raise ReconciliationError(
                f"Database SHA drift: expected={expected_sha256} actual={actual_sha}"
            )
        with sqlite3.connect(str(database), timeout=30.0) as source:
            environment = source.execute(
                "SELECT value FROM meta_data WHERE key='database_environment'"
            ).fetchone()
            if environment is None or environment[0] != "production":
                raise ReconciliationError("Canonical database is not labelled production")
            if source.execute("PRAGMA user_version").fetchone()[0] != 8:
                raise ReconciliationError("Cost reconciliation requires schema v8")
            plan = build_plan(source, effective_date)
        if not plan:
            raise ReconciliationError("No evidence-backed cost-basis repairs are required")

        output_dir.mkdir(parents=True, exist_ok=False)
        pre_backup = _online_backup(database, output_dir / "pre_reconciliation.db")
        rehearsal = _online_backup(database, output_dir / "rehearsal.db")
        with sqlite3.connect(str(rehearsal), timeout=30.0) as candidate:
            candidate.execute("PRAGMA foreign_keys=ON")
            candidate.execute("BEGIN IMMEDIATE")
            apply_plan(candidate, plan)
            candidate.commit()
        check_ledger(rehearsal, effective_date)

        with sqlite3.connect(str(database), timeout=30.0) as production:
            production.execute("PRAGMA foreign_keys=ON")
            production.execute("BEGIN IMMEDIATE")
            if build_plan(production, effective_date) != plan:
                production.rollback()
                raise ReconciliationError("Production reconciliation plan drifted")
            apply_plan(production, plan)
            if production.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                production.rollback()
                raise ReconciliationError("Production integrity check failed")
            if production.execute("PRAGMA foreign_key_check").fetchall():
                production.rollback()
                raise ReconciliationError("Production foreign-key check failed")
            production.commit()
        check_ledger(database, effective_date)
        post_backup = _online_backup(database, output_dir / "post_reconciliation.db")
        manifest = {
            "artifact_type": "tranche-cost-basis-reconciliation",
            "schema_version": 1,
            "status": "completed",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": str(database),
            "effective_date": effective_date,
            "database_sha256_before": actual_sha,
            "database_sha256_after": _sha256_file(database),
            "pre_backup": str(pre_backup),
            "pre_backup_sha256": _sha256_file(pre_backup),
            "post_backup": str(post_backup),
            "post_backup_sha256": _sha256_file(post_backup),
            "repairs": plan,
        }
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--effective-date", required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--output-dir")
    parser.add_argument("--confirm-token")
    parser.add_argument("--apply-production", action="store_true")
    args = parser.parse_args(argv)
    database = Path(args.database).expanduser().resolve()
    if not args.apply_production:
        with sqlite3.connect(str(database), timeout=30.0) as connection:
            plan = build_plan(connection, args.effective_date)
        print(json.dumps({"status": "dry_run", "repairs": plan}, ensure_ascii=False, indent=2))
        return 0
    if not args.expected_sha256 or not args.output_dir:
        parser.error("production apply requires --expected-sha256 and --output-dir")
    manifest = reconcile_production(
        database,
        effective_date=args.effective_date,
        expected_sha256=args.expected_sha256,
        output_dir=args.output_dir,
        confirm_token=args.confirm_token,
        apply_production=True,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
