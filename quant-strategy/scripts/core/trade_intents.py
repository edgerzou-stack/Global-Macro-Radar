"""Market-aware tranche intent planning and legacy-ledger settlement.

Signals are durable intents. Portfolio, cash and trade history change only when
an exact raw open for the eligible market session is supplied by the executor.
"""

import datetime as dt
import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import nullcontext

from core.market import AShareMarket, HKMarket, USMarket
from core.portfolio_limits import MAX_HOLDINGS_PER_STRATEGY
from core.strategy_registry import assert_strategy_not_retired


TRANCHE_AMOUNT = 33_000.0
MARKET_SETTINGS = {
    "A": {"currency": "CNY", "fee_rate": 0.001, "calendar": AShareMarket},
    "HK": {"currency": "HKD", "fee_rate": 0.002, "calendar": HKMarket},
    "US": {"currency": "USD", "fee_rate": 0.0, "calendar": USMarket},
}


class TradeIntentError(RuntimeError):
    pass


def market_for_strategy(strategy_id):
    if "_us_" in strategy_id:
        return "US"
    if "_hk_" in strategy_id:
        return "HK"
    return "A"


def _utc_now_text():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _positive_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _execution_quote(value, *, symbol, market, session):
    """Normalize a price-loader result into auditable execution evidence."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        price = _positive_price(value.get("price"))
        if price is None:
            return None
        price_field = str(value.get("price_field") or "")
        adjustment = str(value.get("adjustment") or "")
        provider = str(value.get("provider") or "")
        if price_field != "open" or adjustment != "raw" or not provider:
            raise TradeIntentError(
                f"Unverifiable execution quote for {symbol}/{session}"
            )
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise TradeIntentError(
                f"Execution quote lacks an evidence payload for {symbol}/{session}"
            )
        payload_json = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        actual_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        supplied_hash = str(value.get("payload_sha256") or actual_hash)
        if supplied_hash != actual_hash:
            raise TradeIntentError(
                f"Execution evidence hash mismatch for {symbol}/{session}"
            )
        return {
            "price": price,
            "price_field": price_field,
            "adjustment": adjustment,
            "provider": provider,
            "payload_json": payload_json,
            "payload_sha256": actual_hash,
        }

    price = _positive_price(value)
    if price is None:
        return None
    payload = {
        "schema_version": 1,
        "symbol": str(symbol),
        "market": str(market),
        "session": str(session),
        "price_field": "open",
        "adjustment": "raw",
        "provider": "injected_price_loader",
        "open": price,
    }
    payload_json = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "price": price,
        "price_field": "open",
        "adjustment": "raw",
        "provider": "injected_price_loader",
        "payload_json": payload_json,
        "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
    }


def _ordered_unique(values):
    result = []
    seen = set()
    for value in values:
        symbol = str(value)
        if symbol and symbol not in seen:
            result.append(symbol)
            seen.add(symbol)
    return result


class TradeIntentLedger:
    def __init__(self, connection: sqlite3.Connection, tranche_amount=TRANCHE_AMOUNT):
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.tranche_amount = float(tranche_amount)
        if not math.isfinite(self.tranche_amount) or self.tranche_amount <= 0:
            raise ValueError("tranche_amount must be positive and finite")
        if not self._table_exists("trade_intents"):
            raise TradeIntentError(
                "trade_intents schema is unavailable; apply v007 before planning"
            )
        if not self._table_exists("trade_execution_evidence"):
            raise TradeIntentError(
                "trade execution evidence schema is unavailable; apply v008 "
                "before planning or settlement"
            )

    def _table_exists(self, name):
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _effective_positions(self, strategy_id):
        quarantine = self._table_exists("quarantine_key_index")
        sql = (
            "SELECT p.id,p.name_or_code,p.entry_date,p.entry_price,p.shares "
            "FROM portfolio p WHERE p.strategy=?"
        )
        if quarantine:
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM quarantine_key_index q "
                "WHERE q.source_table='portfolio' AND q.key_arity=1 "
                "AND q.key_1=CAST(p.id AS TEXT) AND q.key_2='')"
            )
        return {row["name_or_code"]: dict(row) for row in self.conn.execute(sql, (strategy_id,))}

    @staticmethod
    def _eligible_session(signal_date, market):
        signal = dt.date.fromisoformat(str(signal_date))
        calendar = MARKET_SETTINGS[market]["calendar"]()
        return calendar.get_next_trading_date(signal + dt.timedelta(days=1)).isoformat()

    def _account_cash(self, strategy_id):
        row = self.conn.execute(
            "SELECT available_cash FROM strategy_accounts WHERE strategy_id=?",
            (strategy_id,),
        ).fetchone()
        if row is None:
            raise TradeIntentError(f"Missing strategy account: {strategy_id}")
        cash = float(row[0])
        if not math.isfinite(cash) or cash < 0:
            raise TradeIntentError(f"Invalid available cash for {strategy_id}")
        return cash

    def plan_strategy(
        self,
        *,
        run_id,
        signal_date,
        strategy_id,
        ranked_targets,
        reason="quantitative target change",
        manage_transaction=True,
    ):
        if not run_id:
            raise ValueError("run_id is required")
        assert_strategy_not_retired(strategy_id)
        targets = _ordered_unique(ranked_targets)
        if len(targets) > MAX_HOLDINGS_PER_STRATEGY:
            raise TradeIntentError(
                f"{strategy_id} target has {len(targets)} symbols; maximum is "
                f"{MAX_HOLDINGS_PER_STRATEGY}"
            )
        market = market_for_strategy(strategy_id)
        currency = MARKET_SETTINGS[market]["currency"]
        eligible = self._eligible_session(signal_date, market)
        positions = self._effective_positions(strategy_id)
        old_symbols = set(positions)
        target_symbols = set(targets)
        timestamp = _utc_now_text()
        self._account_cash(strategy_id)

        pending_by_action_symbol = {
            (row["action"], row["symbol"]): row
            for row in self.conn.execute(
                "SELECT * FROM trade_intents "
                "WHERE strategy_id=? AND state='PENDING' "
                "AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
                "WHERE s.intent_id=trade_intents.intent_id)",
                (strategy_id,),
            )
        }
        pending_buy_symbols = {
            symbol
            for action, symbol in pending_by_action_symbol
            if action in {"BUY_NEW", "ADD_TRANCHE"} and symbol not in old_symbols
        }
        pending_sell_symbols = {
            symbol
            for action, symbol in pending_by_action_symbol
            if action == "SELL_ALL" and symbol in old_symbols
        }
        conflicting = pending_buy_symbols & pending_sell_symbols
        if conflicting:
            raise TradeIntentError(
                f"{strategy_id} has conflicting pending commitments: "
                f"{sorted(conflicting)}"
            )

        intents = []
        sell_symbols = sorted(old_symbols - target_symbols)
        committed_future_symbols = (
            old_symbols - (pending_sell_symbols | set(sell_symbols))
        ) | pending_buy_symbols
        available_slots = max(
            0, MAX_HOLDINGS_PER_STRATEGY - len(committed_future_symbols)
        )
        candidate_new_buys = [
            symbol
            for symbol in targets
            if symbol not in old_symbols and symbol not in pending_buy_symbols
        ]
        admitted_new_buys = set(candidate_new_buys[:available_slots])
        buy_symbols = [
            symbol
            for symbol in targets
            if symbol in pending_buy_symbols or symbol in admitted_new_buys
        ]
        desired_plan = {
            ("SELL_ALL", symbol, None) for symbol in sell_symbols
        } | {
            ("BUY_NEW", symbol, rank)
            for rank, symbol in enumerate(targets, start=1)
            if symbol in buy_symbols
        }
        existing_plan = {
            (row["action"], row["symbol"], row["target_rank"])
            for row in self.conn.execute(
                "SELECT action,symbol,target_rank FROM trade_intents "
                "WHERE source_run_id=? AND strategy_id=?",
                (run_id, strategy_id),
            )
        }
        if existing_plan and existing_plan != desired_plan:
            raise TradeIntentError(
                f"{run_id}/{strategy_id} is already bound to a different intent plan"
            )
        transaction = self.conn if manage_transaction else nullcontext()
        with transaction:
            for symbol in sell_symbols:
                existing = pending_by_action_symbol.get(("SELL_ALL", symbol))
                if existing is not None:
                    intents.append(dict(existing))
                    continue
                intents.append(
                    self._create_intent(
                        run_id=run_id,
                        signal_date=signal_date,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        market=market,
                        currency=currency,
                        action="SELL_ALL",
                        quantity=max(1, int(positions[symbol].get("shares") or 1)),
                        rank=None,
                        eligible_session=eligible,
                        reserved_cash=0.0,
                        reason=reason,
                        timestamp=timestamp,
                    )
                )
            for rank, symbol in enumerate(targets, start=1):
                if symbol not in buy_symbols:
                    continue
                existing = pending_by_action_symbol.get(("BUY_NEW", symbol))
                if existing is not None:
                    self.conn.execute(
                        "UPDATE trade_intents SET target_rank=?,updated_at=? "
                        "WHERE intent_id=? AND state='PENDING'",
                        (rank, timestamp, existing["intent_id"]),
                    )
                    refreshed = self.conn.execute(
                        "SELECT * FROM trade_intents WHERE intent_id=?",
                        (existing["intent_id"],),
                    ).fetchone()
                    intents.append(dict(refreshed))
                    continue
                intents.append(
                    self._create_intent(
                        run_id=run_id,
                        signal_date=signal_date,
                        strategy_id=strategy_id,
                        symbol=symbol,
                        market=market,
                        currency=currency,
                        action="BUY_NEW",
                        quantity=1,
                        rank=rank,
                        eligible_session=eligible,
                        reserved_cash=self.tranche_amount,
                        reason=reason,
                        timestamp=timestamp,
                        state="PENDING",
                    )
                )
        return intents

    def _create_intent(
        self,
        *,
        run_id,
        signal_date,
        strategy_id,
        symbol,
        market,
        currency,
        action,
        quantity,
        rank,
        eligible_session,
        reserved_cash,
        reason,
        timestamp,
        state="PENDING",
    ):
        key = f"{run_id}:{strategy_id}:{action}:{symbol}"
        existing = self.conn.execute(
            "SELECT * FROM trade_intents WHERE idempotency_key=?", (key,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        intent_id = str(uuid.uuid4())
        self.conn.execute(
            """
            INSERT INTO trade_intents (
                intent_id,idempotency_key,source_run_id,signal_date,strategy_id,
                symbol,market,currency,action,state,tranche_quantity,target_rank,
                eligible_session,reserved_cash,reason,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                intent_id,
                key,
                run_id,
                str(signal_date),
                strategy_id,
                symbol,
                market,
                currency,
                action,
                state,
                quantity,
                rank,
                eligible_session,
                reserved_cash,
                reason,
                timestamp,
                timestamp,
            ),
        )
        return dict(
            self.conn.execute(
                "SELECT * FROM trade_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
        )

    def execute_market_session(
        self,
        *,
        market,
        session_date,
        price_loader,
        fault_injector=None,
    ):
        if market not in MARKET_SETTINGS:
            raise ValueError(f"Unsupported market: {market}")
        session = dt.date.fromisoformat(str(session_date)).isoformat()
        rows = self.conn.execute(
            """
            SELECT * FROM trade_intents
            WHERE market=? AND state='PENDING' AND eligible_session<=?
              AND NOT EXISTS (
                  SELECT 1 FROM trade_intent_supersessions s
                  WHERE s.intent_id=trade_intents.intent_id
              )
            ORDER BY CASE action WHEN 'SELL_ALL' THEN 0 WHEN 'ADD_TRANCHE' THEN 1 ELSE 2 END,
                     COALESCE(target_rank, 0), created_at, intent_id
            """,
            (market, session),
        ).fetchall()
        quotes = {
            row["intent_id"]: _execution_quote(
                price_loader(row["symbol"], market, row["eligible_session"]),
                symbol=row["symbol"],
                market=market,
                session=row["eligible_session"],
            )
            for row in rows
        }
        filled = pending = blocked = 0
        deferred = []
        try:
            self.conn.execute("BEGIN")
            for row in rows:
                quote = quotes[row["intent_id"]]
                price = quote["price"] if quote is not None else None
                fill_session = row["eligible_session"]
                if row["action"] == "SELL_ALL":
                    position = self._effective_positions(row["strategy_id"]).get(row["symbol"])
                    if position is None:
                        self._cancel_intent(row, "position already absent")
                        continue
                    if market == "A" and str(position.get("entry_date", ""))[:10] >= fill_session:
                        next_session = self._eligible_session(fill_session, market)
                        self.conn.execute(
                            "UPDATE trade_intents SET eligible_session=?,updated_at=? "
                            "WHERE intent_id=? AND state='PENDING'",
                            (next_session, _utc_now_text(), row["intent_id"]),
                        )
                        pending += 1
                        continue
                    if price is None:
                        pending += 1
                        deferred.append(
                            {
                                "intent_id": row["intent_id"],
                                "strategy_id": row["strategy_id"],
                                "symbol": row["symbol"],
                                "action": row["action"],
                                "eligible_session": row["eligible_session"],
                                "reason": "exact_session_open_unavailable",
                            }
                        )
                        continue
                    self._fill_sell(row, position, quote, fill_session)
                    if fault_injector:
                        fault_injector("after_portfolio")
                    filled += 1
                else:
                    positions = self._effective_positions(row["strategy_id"])
                    if row["symbol"] in positions:
                        self._cancel_intent(row, "position already present")
                        continue
                    if len(positions) >= MAX_HOLDINGS_PER_STRATEGY:
                        blocked += 1
                        continue
                    if price is None:
                        pending += 1
                        deferred.append(
                            {
                                "intent_id": row["intent_id"],
                                "strategy_id": row["strategy_id"],
                                "symbol": row["symbol"],
                                "action": row["action"],
                                "eligible_session": row["eligible_session"],
                                "reason": "exact_session_open_unavailable",
                            }
                        )
                        continue
                    if not self._fill_buy(row, quote, fill_session):
                        blocked += 1
                        continue
                    if fault_injector:
                        fault_injector("after_portfolio")
                    filled += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {
            "filled": filled,
            "pending": pending,
            "blocked": blocked,
            "deferred": deferred,
        }

    def _fill_sell(self, intent, position, quote, session):
        strategy = intent["strategy_id"]
        symbol = intent["symbol"]
        price = quote["price"]
        shares = max(1, int(position.get("shares") or 1))
        entry_price = float(position["entry_price"])
        fee_rate = MARKET_SETTINGS[intent["market"]]["fee_rate"]
        pnl = price / entry_price - 1.0 - fee_rate
        invested = self.tranche_amount * shares
        returned = invested * (1.0 + pnl)
        self.conn.execute(
            "INSERT INTO trade_history "
            "(strategy,name_or_code,entry_date,entry_price,exit_date,exit_price,"
            "pnl,reason,shares) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                strategy,
                symbol,
                position.get("entry_date"),
                entry_price,
                session,
                price,
                pnl,
                f"[INTENT:{intent['intent_id']}] {intent['reason'] or ''}".strip(),
                shares,
            ),
        )
        self.conn.execute("DELETE FROM portfolio WHERE id=?", (position["id"],))
        self.conn.execute(
            "UPDATE strategy_accounts SET available_cash=available_cash+?, "
            "total_capital=total_capital+? WHERE strategy_id=?",
            (returned, invested * pnl, strategy),
        )
        self._record_execution_evidence(intent, quote, session)
        self._mark_filled(intent, price, fee_rate, pnl, session)

    def _fill_buy(self, intent, quote, session):
        strategy = intent["strategy_id"]
        price = quote["price"]
        quantity = int(intent["tranche_quantity"])
        cost = self.tranche_amount * quantity
        result = self.conn.execute(
            "UPDATE strategy_accounts SET available_cash=available_cash-? "
            "WHERE strategy_id=? AND available_cash>=?",
            (cost, strategy, cost),
        )
        if result.rowcount != 1:
            return False
        self.conn.execute(
            "INSERT INTO portfolio "
            "(strategy,name_or_code,entry_date,entry_price,weight,shares) "
            "VALUES (?,?,?,?,0,?)",
            (strategy, intent["symbol"], session, price, quantity),
        )
        self.conn.execute(
            "INSERT INTO portfolio_snapshots "
            "(snapshot_date,strategy,name_or_code,weight) VALUES (?,?,?,0)",
            (session, strategy, intent["symbol"]),
        )
        self._record_execution_evidence(intent, quote, session)
        self._mark_filled(intent, price, 0.0, None, session)
        return True

    def _record_execution_evidence(self, intent, quote, session):
        self.conn.execute(
            """
            INSERT INTO trade_execution_evidence (
                intent_id,symbol,market,execution_session,price_field,adjustment,
                provider,observed_at,payload_sha256,payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                intent["intent_id"],
                intent["symbol"],
                intent["market"],
                session,
                quote["price_field"],
                quote["adjustment"],
                quote["provider"],
                _utc_now_text(),
                quote["payload_sha256"],
                quote["payload_json"],
            ),
        )

    def _mark_filled(self, intent, price, fee_rate, pnl, session):
        timestamp = f"{session}T09:30:00"
        self.conn.execute(
            "UPDATE trade_intents SET state='FILLED',reserved_cash=0,updated_at=?,"
            "executed_at=?,execution_price=?,fee_rate=?,realized_pnl=? "
            "WHERE intent_id=? AND state='PENDING'",
            (timestamp, timestamp, price, fee_rate, pnl, intent["intent_id"]),
        )

    def _cancel_intent(self, intent, reason):
        self.conn.execute(
            "UPDATE trade_intents SET state='CANCELLED',reserved_cash=0,reason=?,"
            "updated_at=? WHERE intent_id=? AND state='PENDING'",
            (reason, _utc_now_text(), intent["intent_id"]),
        )
