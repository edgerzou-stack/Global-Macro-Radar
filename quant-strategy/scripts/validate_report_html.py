"""Validate that rendered ledger claims exactly match the bound SQLite database."""

import argparse
import json
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bs4 import BeautifulSoup
from core.quarantine import quarantine_filter
from db_utils import test_strategy_filter


STRATEGY_TITLES = {
    "A股核心红利精选": "dividend_a_stock",
    "A股高增成长精选": "growth_a_stock",
    "美股高增成长精选": "growth_us_stock",
    "港股高增成长精选": "growth_hk_stock",
    "A股热点突击 (个股)": "hot_spot_a_stock",
    "美股热点突击 (个股)": "hot_spot_us_stock",
    "港股热点突击 (个股)": "hot_spot_hk_stock",
}
BANNED_LEDGER_CLAIMS = (
    "当前持仓列表",
    "已完成交割",
    "历史平仓交割单明细",
)
NAV_RUN_STATUS_PREFIX = "nav_run_status:"
SETTLEMENT_RUN_STATUS_PREFIX = "settlement_run_status:"
NAV_STATUS_LABELS = {
    "fresh": "本次运行已重估",
    "certified_carry_forward": "沿用最近认证快照",
    "unavailable": "不可估值",
    "status_record_missing": "状态记录缺失",
}


def _strategy_display(strategy_id):
    strategy_id = str(strategy_id)
    title = next(
        (title for title, sid in STRATEGY_TITLES.items() if sid == strategy_id),
        strategy_id,
    )
    return f"{title} ({strategy_id})"


def _retired_performance_chart_errors(soup):
    report_text = soup.get_text(" ", strip=True)
    errors = []
    if soup.find("img", alt="pnl_chart_all.png") is not None:
        errors.append(
            "retired pnl_chart_all.png is present; cumulative performance must "
            "come from certified NAV"
        )
    if (
        "账户净值与回测曲线" in report_text
        or "各策略等权累计净收益曲线综合对比" in report_text
        or "Master Chart" in report_text
    ):
        errors.append(
            "retired trade-return aggregation chart text is present"
        )
    return errors


class ReportValidationError(RuntimeError):
    pass


def _money(text):
    normalized = re.sub(r"[^0-9.\-]", "", text)
    try:
        return Decimal(normalized).quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise ReportValidationError(f"Invalid money value in report: {text!r}") from error


def _next_table_or_empty(heading):
    if heading is None:
        return None
    node = heading.find_next_sibling()
    while node is not None and node.name not in {"h3", "h4", "table"}:
        if node.name == "p" and "暂无" in node.get_text(" ", strip=True):
            return None
        node = node.find_next_sibling()
    return node if node is not None and node.name == "table" else None


def _first_column(table):
    if table is None:
        return []
    return [
        row.find_all("td")[0].get_text(" ", strip=True)
        for row in table.select("tbody tr")
        if row.find_all("td")
    ]


def _table_row_count(table):
    return len(table.select("tbody tr")) if table is not None else 0


def _table_cells(table):
    if table is None:
        return []
    return [
        [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        for row in table.select("tbody tr")
    ]


def _pending_intent_identity(cells):
    """Project a rendered row onto immutable ledger identity fields.

    Stock names and explanatory notes are presentation fields. They may be
    localized without weakening validation of the underlying intent.
    """
    if len(cells) < 9:
        raise ValueError("pending intent row must contain at least 9 cells")
    return (
        cells[0],
        cells[1],
        cells[2],
        cells[3],
        cells[4],
        cells[6],
        cells[7],
        cells[8],
    )


def _pending_intent_rows_match(displayed, database):
    """Compare ledger identities without coupling validity to display order."""
    return sorted(displayed) == sorted(database)


def _section_text(heading):
    parts = []
    for node in heading.next_siblings:
        if getattr(node, "name", None) in {"h2", "h3"}:
            break
        if hasattr(node, "get_text"):
            parts.append(node.get_text(" ", strip=True))
    return " ".join(parts)


def _database_execution_counts(connection, strategy_id):
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if not {"orders", "fills"}.issubset(tables):
        return None
    orders = connection.execute(
        "SELECT COUNT(*) FROM orders WHERE strategy_id=?", (strategy_id,)
    ).fetchone()[0]
    fills = connection.execute(
        "SELECT COUNT(*) FROM fills f JOIN orders o ON o.order_id=f.order_id "
        "WHERE o.strategy_id=?",
        (strategy_id,),
    ).fetchone()[0]
    return orders, fills


def _load_run_status(connection, prefix, run_id):
    if not run_id:
        return None
    row = connection.execute(
        "SELECT value FROM meta_data WHERE key=?",
        (prefix + run_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        raise ReportValidationError(
            f"invalid {prefix} payload identity for run {run_id}"
        )
    return payload


def _expected_nav_status(nav_status, strategy_id):
    if not nav_status:
        return {
            "status": "status_record_missing",
            "snapshot_date": None,
            "valuation_session": None,
            "failure_reason": "本次运行没有持久化 NAV 状态记录。",
        }
    return nav_status.get("strategies", {}).get(
        strategy_id,
        {
            "status": "status_record_missing",
            "snapshot_date": None,
            "valuation_session": None,
            "failure_reason": "该策略缺少本次运行的 NAV 状态。",
        },
    )


def validate_report(html_path, database_path):
    html_path = Path(html_path).expanduser().resolve()
    database_path = Path(database_path).expanduser().resolve()
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    report_text = soup.get_text(" ", strip=True)
    errors = _retired_performance_chart_errors(soup)

    for claim in BANNED_LEDGER_CLAIMS:
        if claim in report_text:
            errors.append(f"banned ambiguous ledger claim is present: {claim}")

    with sqlite3.connect(database_path) as connection:
        metadata = soup.find(id="report-metadata")
        run_id = metadata.get("data-run-id", "") if metadata else ""
        signal_date = metadata.get("data-signal-date", "") if metadata else ""
        generated_at = metadata.get("data-generated-at", "") if metadata else ""
        if metadata is None:
            errors.append("report metadata block is missing")
        elif not run_id or not signal_date or not generated_at:
            errors.append("report metadata identity is incomplete")

        try:
            nav_status = _load_run_status(
                connection, NAV_RUN_STATUS_PREFIX, run_id
            )
            settlement_status = _load_run_status(
                connection, SETTLEMENT_RUN_STATUS_PREFIX, run_id
            )
        except (json.JSONDecodeError, ReportValidationError) as error:
            errors.append(str(error))
            nav_status = None
            settlement_status = None

        if nav_status and str(nav_status.get("report_date")) != signal_date:
            errors.append(
                "NAV status report date does not match report signal date: "
                f"{nav_status.get('report_date')} != {signal_date}"
            )
        if (
            settlement_status
            and str(settlement_status.get("session_date")) != signal_date
        ):
            errors.append(
                "settlement session date does not match report signal date: "
                f"{settlement_status.get('session_date')} != {signal_date}"
            )

        account_filter, account_parameters, _ = quarantine_filter(
            connection, "strategy_accounts"
        )
        account_test_filter, account_test_parameters = test_strategy_filter(
            "strategy_id"
        )
        account_rows = {
            row[0]: (Decimal(str(row[1])).quantize(Decimal("0.01")),
                     Decimal(str(row[2])).quantize(Decimal("0.01")))
            for row in connection.execute(
                "SELECT strategy_id,total_capital,available_cash "
                "FROM strategy_accounts WHERE 1=1"
                + account_filter
                + account_test_filter,
                account_parameters + account_test_parameters,
            )
        }
        overview = soup.find("h2", string=lambda value: value and "台账概览" in value)
        overview_table = overview.find_next("table") if overview else None
        displayed_accounts = {}
        if overview_table:
            for row in overview_table.select("tbody tr"):
                cells = row.find_all("td")
                if len(cells) >= 3:
                    strategy_id = cells[0].get_text(" ", strip=True)
                    displayed_accounts[strategy_id] = (
                        _money(cells[1].get_text(" ", strip=True)),
                        _money(cells[2].get_text(" ", strip=True)),
                    )
        if displayed_accounts != account_rows:
            errors.append(
                "account overview does not match strategy_accounts: "
                f"displayed={displayed_accounts}, database={account_rows}"
            )

        nav_table = soup.find("table", id="nav-status-table")
        displayed_nav_status = {}
        if nav_table:
            for row in nav_table.select("tbody tr"):
                cells = row.find_all("td")
                strategy_id = row.get("data-strategy", "")
                if len(cells) < 9 or not strategy_id:
                    errors.append("malformed NAV status row")
                    continue
                displayed_nav_status[strategy_id] = {
                    "status": row.get("data-nav-status", ""),
                    "certified_nav": cells[3].get_text(" ", strip=True),
                    "status_label": cells[5].get_text(" ", strip=True),
                    "snapshot_date": row.get("data-snapshot-date", ""),
                    "snapshot_date_text": cells[6].get_text(" ", strip=True),
                    "valuation_session": row.get(
                        "data-valuation-session", ""
                    ),
                    "valuation_session_text": cells[7].get_text(" ", strip=True),
                    "failure_reason": cells[8].get_text(" ", strip=True),
                }
        else:
            errors.append("NAV status table is missing")

        if set(displayed_nav_status) != set(account_rows):
            errors.append(
                "NAV status strategy set does not match active accounts: "
                f"displayed={sorted(displayed_nav_status)}, "
                f"database={sorted(account_rows)}"
            )
        for strategy_id, (account_capital, account_cash) in account_rows.items():
            displayed = displayed_nav_status.get(strategy_id)
            if displayed is None:
                continue
            expected = _expected_nav_status(nav_status, strategy_id)
            expected_status = expected.get("status", "status_record_missing")
            expected_snapshot = str(expected.get("snapshot_date") or "")
            expected_session = str(expected.get("valuation_session") or "")
            expected_reason = str(expected.get("failure_reason") or "-")
            if displayed["status"] != expected_status:
                errors.append(f"{strategy_id} NAV status mismatch")
            if displayed["status_label"] != NAV_STATUS_LABELS.get(
                expected_status, expected_status
            ):
                errors.append(f"{strategy_id} NAV status label mismatch")
            if (
                displayed["snapshot_date"] != expected_snapshot
                or displayed["snapshot_date_text"] != (expected_snapshot or "-")
            ):
                errors.append(f"{strategy_id} NAV snapshot date mismatch")
            if (
                displayed["valuation_session"] != expected_session
                or displayed["valuation_session_text"] != (expected_session or "-")
            ):
                errors.append(f"{strategy_id} valuation session mismatch")
            if displayed["failure_reason"] != expected_reason:
                errors.append(f"{strategy_id} NAV reason mismatch")

            current_cash = expected.get("current_available_cash")
            if current_cash is not None and _money(str(current_cash)) != account_cash:
                errors.append(f"{strategy_id} NAV status current cash mismatch")
            status_nav = expected.get("nav")
            expected_nav_text = (
                _money(str(status_nav)) if status_nav is not None else None
            )
            displayed_nav_text = (
                None
                if displayed["certified_nav"] == "-"
                else _money(displayed["certified_nav"])
            )
            if displayed_nav_text != expected_nav_text:
                errors.append(f"{strategy_id} certified NAV display mismatch")
            if (
                expected_status == "fresh"
                and status_nav is not None
                and _money(str(status_nav)) != account_capital
            ):
                errors.append(f"{strategy_id} NAV status capital mismatch")

        settlement_table = soup.find("table", id="settlement-status-table")
        if settlement_status is None:
            missing_disclosure = soup.find(
                id="settlement-status",
                attrs={"data-record-present": "false"},
            )
            if missing_disclosure is None or settlement_table is not None:
                errors.append("missing settlement status is not disclosed exactly")
        else:
            expected_markets = []
            for market, item in sorted(
                settlement_status.get("markets", {}).items()
            ):
                status = str(item.get("status") or "")
                strategies = item.get("strategies") or {}
                if strategies:
                    aggregate_filled = aggregate_pending = 0
                    aggregate_blocked = aggregate_deferred = 0
                    for strategy_id, detail in sorted(strategies.items()):
                        filled = detail.get("filled_by_action", {})
                        pending = detail.get("pending_by_action", {})
                        filled_buy = int(filled.get("BUY_NEW", 0)) + int(
                            filled.get("ADD_TRANCHE", 0)
                        )
                        filled_sell = int(filled.get("SELL_ALL", 0))
                        pending_buy = int(pending.get("BUY_NEW", 0)) + int(
                            pending.get("ADD_TRANCHE", 0)
                        )
                        pending_sell = int(pending.get("SELL_ALL", 0))
                        blocked = int(detail.get("blocked", 0))
                        deferred = int(detail.get("deferred", 0))
                        aggregate_filled += filled_buy + filled_sell
                        aggregate_pending += pending_buy + pending_sell
                        aggregate_blocked += blocked
                        aggregate_deferred += deferred
                        expected_markets.append(
                            (
                                market,
                                str(strategy_id),
                                _strategy_display(strategy_id),
                                status,
                                str(filled_buy),
                                str(filled_sell),
                                str(pending_buy),
                                str(pending_sell),
                                blocked,
                                deferred,
                            )
                        )
                    if aggregate_filled != int(item.get("filled", 0)):
                        errors.append(f"{market} strategy filled totals mismatch")
                    if aggregate_pending != int(item.get("pending", 0)):
                        errors.append(f"{market} strategy pending totals mismatch")
                    if aggregate_blocked != int(item.get("blocked", 0)):
                        errors.append(f"{market} strategy blocked totals mismatch")
                    if aggregate_deferred != len(item.get("deferred", [])):
                        errors.append(f"{market} strategy deferred totals mismatch")
                else:
                    expected_markets.append(
                        (
                            market,
                            "",
                            "未提供策略拆分",
                            status,
                            f"未拆分（合计 {int(item.get('filled', 0))}）",
                            "未拆分",
                            f"未拆分（合计 {int(item.get('pending', 0))}）",
                            "未拆分",
                            int(item.get("blocked", 0)),
                            len(item.get("deferred", [])),
                        )
                    )
            displayed_markets = []
            if settlement_table:
                if settlement_table.get("data-session-date", "") != str(
                    settlement_status.get("session_date") or ""
                ):
                    errors.append("settlement table session date mismatch")
                for row in settlement_table.select("tbody tr"):
                    cells = row.find_all("td")
                    market = row.get("data-market", "")
                    if len(cells) < 10 or not market:
                        errors.append("malformed settlement status row")
                        continue
                    displayed_markets.append(
                        (
                            market,
                            row.get("data-strategy", ""),
                            cells[1].get_text(" ", strip=True),
                            row.get("data-status", ""),
                            cells[4].get_text(" ", strip=True),
                            cells[5].get_text(" ", strip=True),
                            cells[6].get_text(" ", strip=True),
                            cells[7].get_text(" ", strip=True),
                            int(cells[8].get_text(" ", strip=True)),
                            int(cells[9].get_text(" ", strip=True)),
                        )
                    )
                    if cells[2].get_text(" ", strip=True) != str(
                        settlement_status.get("session_date") or ""
                    ):
                        errors.append(f"{market} settlement session label mismatch")
                    if cells[3].get_text(" ", strip=True) != row.get(
                        "data-status", ""
                    ):
                        errors.append(f"{market} settlement status label mismatch")
            else:
                errors.append("settlement status table is missing")
            if displayed_markets != expected_markets:
                errors.append(
                    "settlement status does not match database: "
                    f"displayed={displayed_markets}, database={expected_markets}"
                )

        checked_strategies = []
        portfolio_filter, portfolio_parameters, _ = quarantine_filter(
            connection, "portfolio"
        )
        portfolio_test_filter, portfolio_test_parameters = test_strategy_filter(
            "strategy"
        )
        trade_filter, trade_parameters, _ = quarantine_filter(
            connection, "trade_history"
        )
        trade_test_filter, trade_test_parameters = test_strategy_filter("strategy")
        for heading in soup.find_all("h3"):
            title = heading.get_text(" ", strip=True)
            strategy_id = STRATEGY_TITLES.get(title)
            if not strategy_id:
                continue
            checked_strategies.append(strategy_id)

            holdings_heading = heading.find_next(
                "h4", string="实际持仓（legacy portfolio）"
            )
            if holdings_heading is None:
                errors.append(f"{strategy_id} actual-holdings heading is missing")
            holdings_table = _next_table_or_empty(holdings_heading)
            displayed_holdings = _first_column(holdings_table)
            database_holdings = [
                row[0]
                for row in connection.execute(
                    "SELECT name_or_code FROM portfolio WHERE strategy=?"
                    + portfolio_filter
                    + portfolio_test_filter
                    + " ORDER BY name_or_code",
                    (strategy_id,)
                    + portfolio_parameters
                    + portfolio_test_parameters,
                )
            ]
            if displayed_holdings != database_holdings:
                errors.append(
                    f"{strategy_id} holdings mismatch: "
                    f"displayed={displayed_holdings}, database={database_holdings}"
                )

            intent_heading = heading.find_next(
                "h4", string="待交割指令（v7 trade_intents）"
            )
            if intent_heading is None:
                errors.append(f"{strategy_id} pending-intents heading is missing")
            intent_table = _next_table_or_empty(intent_heading)
            displayed_intents = [
                tuple(cells)
                for cells in _table_cells(intent_table)
                if len(cells) >= 10
            ]
            if any(not cells[5] for cells in displayed_intents):
                errors.append(f"{strategy_id} pending intent has no stock name")
            displayed_intent_keys = [
                _pending_intent_identity(cells)
                for cells in displayed_intents
            ]

            has_v7 = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='trade_intents'"
            ).fetchone()
            if has_v7:
                has_supersessions = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='trade_intent_supersessions'"
                ).fetchone()
                supersession_filter = (
                    " AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
                    "WHERE s.intent_id=trade_intents.intent_id)"
                    if has_supersessions
                    else ""
                )
                action_labels = {
                    "SELL_ALL": "卖出",
                    "BUY_NEW": "买入",
                    "ADD_TRANCHE": "加仓",
                }
                database_intents = [
                    (
                        _strategy_display(strategy_id),
                        str(intent_id),
                        str(market),
                        action_labels.get(action, action),
                        str(symbol),
                        str(signal_date),
                        str(eligible),
                        "待交割",
                    )
                    for (
                        intent_id,
                        source_run_id,
                        signal_date,
                        market,
                        symbol,
                        action,
                        eligible,
                        reason,
                    ) in connection.execute(
                        "SELECT intent_id,source_run_id,signal_date,market,symbol,"
                        "action,eligible_session,reason FROM trade_intents "
                        "WHERE strategy_id=? AND state='PENDING' "
                        + supersession_filter
                        + " "
                        "ORDER BY CASE action WHEN 'SELL_ALL' THEN 0 ELSE 1 END,"
                        "COALESCE(target_rank,0),symbol",
                        (strategy_id,),
                    )
                ]
                if not _pending_intent_rows_match(
                    displayed_intent_keys,
                    database_intents,
                ):
                    errors.append(
                        f"{strategy_id} pending intents mismatch: "
                        f"displayed={displayed_intent_keys}, database={database_intents}"
                    )
            elif "v7 交割意图账本尚不可用" not in _section_text(heading):
                errors.append(f"{strategy_id} missing unavailable v7 disclosure")

            filled_heading = heading.find_next(
                "h4",
                string=re.compile(r"交易执行与历史成交明细|已交割成交明细"),
            )
            if filled_heading is None:
                errors.append(f"{strategy_id} filled-executions heading is missing")
            filled_table = _next_table_or_empty(filled_heading)

            is_unified = filled_heading is not None and "交易执行与历史成交明细" in filled_heading.get_text()
            if is_unified:
                displayed_filled = [
                    (
                        cells[0],
                        cells[1],
                        cells[2],
                        cells[3],
                        cells[4],
                        cells[6],
                        cells[7],
                        cells[8],
                        cells[9],
                        cells[10],
                        cells[11].lower(),
                    )
                    for cells in _table_cells(filled_table)
                    if len(cells) >= 12
                ]
            else:
                displayed_filled = [
                    (
                        cells[0],
                        cells[1],
                        cells[2],
                        cells[3],
                        cells[4],
                        cells[6],
                        cells[7],
                        cells[8],
                        cells[9],
                        cells[10],
                        cells[11],
                        cells[12],
                        cells[13].lower(),
                    )
                    for cells in _table_cells(filled_table)
                    if len(cells) >= 14
                ]
            if has_v7:
                has_evidence = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='trade_execution_evidence'"
                ).fetchone()
                supersession_filter = (
                    " AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
                    "WHERE s.intent_id=i.intent_id)"
                    if has_supersessions
                    else ""
                )
                intent_test_filter, intent_test_parameters = test_strategy_filter(
                    "strategy_id"
                )
                filled_rows = []
                if has_evidence:
                    filled_rows = connection.execute(
                        "SELECT i.intent_id,i.symbol,i.market,i.action,i.tranche_quantity,"
                        "i.eligible_session,i.execution_price,e.execution_session,"
                        "e.price_field,e.adjustment,e.provider,e.observed_at,"
                        "e.payload_sha256 "
                        "FROM trade_intents i "
                        "LEFT JOIN trade_execution_evidence e "
                        "ON e.intent_id=i.intent_id "
                        "WHERE i.strategy_id=? AND i.state='FILLED'"
                        + supersession_filter
                        + intent_test_filter
                        + " ORDER BY e.execution_session,i.executed_at,i.intent_id",
                        (strategy_id,) + intent_test_parameters,
                    ).fetchall()
                else:
                    active_filled_count = connection.execute(
                        "SELECT COUNT(*) FROM trade_intents i "
                        "WHERE i.strategy_id=? AND i.state='FILLED'"
                        + supersession_filter
                        + intent_test_filter,
                        (strategy_id,) + intent_test_parameters,
                    ).fetchone()[0]
                    if active_filled_count:
                        errors.append(
                            f"{strategy_id} has active FILLED intents without "
                            "the v8 execution evidence table"
                        )

                action_labels = {
                    "SELL_ALL": "卖出",
                    "BUY_NEW": "买入",
                    "ADD_TRANCHE": "加仓",
                }
                database_filled = []
                for (
                    intent_id,
                    symbol,
                    market,
                    action,
                    quantity,
                    eligible_session,
                    execution_price,
                    execution_session,
                    price_field,
                    adjustment,
                    provider,
                    observed_at,
                    payload_sha256,
                ) in filled_rows:
                    if any(
                        value is None
                        for value in (
                            execution_session,
                            price_field,
                            adjustment,
                            provider,
                            observed_at,
                            payload_sha256,
                        )
                    ):
                        errors.append(
                            f"{strategy_id}/{symbol} FILLED intent {intent_id} "
                            "is missing execution evidence"
                        )
                        continue
                    if execution_price is None or float(execution_price) <= 0:
                        errors.append(
                            f"{strategy_id}/{symbol} FILLED intent has no "
                            "positive execution price"
                        )
                        continue
                    if execution_session != eligible_session:
                        errors.append(
                            f"{strategy_id}/{symbol} execution session does not "
                            "match eligible session"
                        )
                    if price_field != "open" or adjustment != "raw":
                        errors.append(
                            f"{strategy_id}/{symbol} execution evidence is not "
                            "an unadjusted open price"
                        )
                    digest = str(payload_sha256).lower()
                    if not re.fullmatch(r"[0-9a-f]{64}", digest):
                        errors.append(
                            f"{strategy_id}/{symbol} execution evidence has an "
                            "invalid payload SHA-256"
                        )
                    if is_unified:
                        continue
                    else:
                        database_filled.append(
                            (
                                _strategy_display(strategy_id),
                                str(intent_id),
                                str(market),
                                action_labels.get(action, action),
                                str(symbol),
                                "已按开盘价结算",
                                str(execution_session),
                                f"{float(execution_price):.2f}",
                                str(int(quantity)),
                                str(price_field),
                                str(adjustment),
                                str(provider),
                                digest,
                            )
                        )
                if not is_unified:
                    if displayed_filled != database_filled:
                        errors.append(
                            f"{strategy_id} filled executions mismatch: "
                            f"displayed={displayed_filled}, database={database_filled}"
                        )
                else:
                    history_rows = connection.execute(
                        "SELECT id,name_or_code,exit_date,exit_price,pnl,reason,shares "
                        "FROM trade_history WHERE strategy=?"
                        + trade_filter
                        + trade_test_filter
                        + " ORDER BY id",
                        (strategy_id,)
                        + trade_parameters
                        + trade_test_parameters,
                    ).fetchall()
                    history_by_intent = {}
                    for history_row in history_rows:
                        match = re.search(
                            r"\[INTENT:([^\]]+)\]", str(history_row[5] or "")
                        )
                        if not match:
                            continue
                        linked_intent = match.group(1)
                        if linked_intent in history_by_intent:
                            errors.append(
                                f"multiple trade_history rows claim intent "
                                f"{linked_intent}"
                            )
                        history_by_intent[linked_intent] = history_row

                    expected_unified = []
                    consumed_history_ids = set()
                    for (
                        intent_id,
                        symbol,
                        market,
                        action,
                        quantity,
                        _eligible_session,
                        execution_price,
                        execution_session,
                        _price_field,
                        _adjustment,
                        provider,
                        _observed_at,
                        payload_sha256,
                    ) in filled_rows:
                        pnl_text = "-"
                        if action == "SELL_ALL":
                            history_row = history_by_intent.get(str(intent_id))
                            if history_row is None:
                                errors.append(
                                    f"{strategy_id}/{symbol} SELL_ALL {intent_id} "
                                    "has no exact trade_history link"
                                )
                            else:
                                (
                                    history_id,
                                    history_symbol,
                                    history_exit_date,
                                    history_exit_price,
                                    history_pnl,
                                    _history_reason,
                                    _history_shares,
                                ) = history_row
                                consumed_history_ids.add(int(history_id))
                                if (
                                    str(history_symbol) != str(symbol)
                                    or str(history_exit_date)
                                    != str(execution_session)
                                    or abs(
                                        float(history_exit_price)
                                        - float(execution_price)
                                    )
                                    >= 0.01
                                ):
                                    errors.append(
                                        f"{strategy_id}/{symbol} SELL_ALL {intent_id} "
                                        "does not match linked trade_history"
                                    )
                                if history_pnl is not None:
                                    pnl_text = (
                                        f"{float(history_pnl) * 100:+.2f}%"
                                    )
                        expected_unified.append(
                            (
                                _strategy_display(strategy_id),
                                str(intent_id),
                                str(market),
                                action_labels.get(action, action),
                                str(symbol),
                                str(execution_session),
                                f"{float(execution_price):.2f}",
                                pnl_text,
                                str(int(quantity)),
                                str(provider),
                                str(payload_sha256).lower(),
                            )
                        )

                    market = (
                        "A"
                        if strategy_id.endswith("_a_stock")
                        else "HK"
                        if strategy_id.endswith("_hk_stock")
                        else "US"
                    )
                    for (
                        history_id,
                        history_symbol,
                        history_exit_date,
                        history_exit_price,
                        history_pnl,
                        _history_reason,
                        history_shares,
                    ) in history_rows:
                        if int(history_id) in consumed_history_ids:
                            continue
                        exit_price_text = (
                            f"{float(history_exit_price):.2f}"
                            if history_exit_price is not None
                            and float(history_exit_price) > 0
                            else "N/A"
                        )
                        pnl_text = (
                            f"{float(history_pnl or 0.0) * 100:+.2f}%"
                        )
                        expected_unified.append(
                            (
                                _strategy_display(strategy_id),
                                f"legacy-trade:{int(history_id)}",
                                market,
                                "卖出平仓",
                                str(history_symbol),
                                str(history_exit_date),
                                exit_price_text,
                                pnl_text,
                                str(int(history_shares or 1)),
                                "Legacy",
                                "无（历史账本未保存原始行情摘要）".lower(),
                            )
                        )
                    expected_unified.sort(
                        key=lambda row: (row[5], row[1]), reverse=True
                    )
                    if displayed_filled != expected_unified:
                        errors.append(
                            f"{strategy_id} unified executions mismatch: "
                            f"displayed={displayed_filled}, database={expected_unified}"
                        )
            elif "v7/v8 成交证据账本尚不可用" not in _section_text(heading):
                errors.append(
                    f"{strategy_id} missing unavailable filled-execution disclosure"
                )

            history_heading = heading.find_next(
                "h4",
                string="内部账本历史平仓记录（legacy trade_history）",
            )
            if history_heading is not None:
                history_table = _next_table_or_empty(history_heading)
                displayed_trade_count = _table_row_count(history_table)
                database_trade_count = connection.execute(
                    "SELECT COUNT(*) FROM trade_history WHERE strategy=?"
                    + trade_filter
                    + trade_test_filter,
                    (strategy_id,) + trade_parameters + trade_test_parameters,
                ).fetchone()[0]
                if displayed_trade_count != database_trade_count:
                    errors.append(
                        f"{strategy_id} legacy trade count mismatch: "
                        f"displayed={displayed_trade_count}, database={database_trade_count}"
                    )

            execution_counts = _database_execution_counts(connection, strategy_id)
            section_text = _section_text(heading)
            if execution_counts is None:
                if "v6 执行账本尚不可用" not in section_text:
                    errors.append(f"{strategy_id} missing unavailable v6 disclosure")
            else:
                orders, fills = execution_counts
                expected = f"v6 执行账本：{orders} 张订单、{fills} 笔成交"
                if expected not in section_text:
                    errors.append(
                        f"{strategy_id} execution disclosure mismatch: expected {expected}"
                    )

    if errors:
        raise ReportValidationError("; ".join(errors))
    return {
        "status": "ok",
        "html": str(html_path),
        "database": str(database_path),
        "strategies_checked": checked_strategies,
        "accounts_checked": len(account_rows),
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", required=True)
    parser.add_argument("--database", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    result = validate_report(args.html, args.database)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
