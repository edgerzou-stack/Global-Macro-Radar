import json
import sys
import os
import base64
import hashlib
import datetime as dt
import html as html_lib
import re
from core.diagnose import diagnose_elimination
from core.quarantine import quarantine_filter

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from llm_utils import call_llm
except ImportError:
    call_llm = None
try:
    from get_stock_name import get_stock_name
except ImportError:
    get_stock_name = lambda x: x

STRAT_NAMES = {
    "dividend_a_stock": "A股核心红利精选",
    "growth_a_stock": "A股高增成长精选",
    "growth_us_stock": "美股高增成长精选",
    "growth_hk_stock": "港股高增成长精选",
    "hot_spot_a_stock": "A股热点突击 (个股)",
    "hot_spot_us_stock": "美股热点突击 (个股)",
    "hot_spot_hk_stock": "港股热点突击 (个股)",
}

STRAT_REASONS = {
    "dividend_a_stock": "红利避险 (高股息与稳定分红)",
    "growth_a_stock": "高增成长 (营收利润连续增长及动量)",
    "growth_us_stock": "高增成长 (营收利润连续增长及动量)",
    "growth_hk_stock": "高增成长 (营收利润连续增长及动量)",
    "hot_spot_a_stock": "热点突击 (新闻突发热度及资金流向)",
    "hot_spot_us_stock": "热点突击 (新闻突发热度及资金流向)",
    "hot_spot_hk_stock": "热点突击 (新闻突发热度及资金流向)",
}

POSITION_HEADERS = ["股票代码", "股票简称", "买入日期", "买入价格", "仓位份数"]
INTENT_HEADERS = [
    "发起策略",
    "意图ID",
    "市场",
    "操作",
    "股票代码",
    "股票名称",
    "信号日",
    "最早交割日",
    "状态",
    "说明",
]
FILLED_INTENT_HEADERS = [
    "发起策略",
    "意图ID",
    "市场",
    "操作",
    "股票代码",
    "股票名称",
    "状态",
    "成交日",
    "成交价格",
    "投入份数",
    "价格字段",
    "复权口径",
    "行情来源",
    "证据SHA-256",
]
UNIFIED_TRADE_HEADERS = [
    "发起策略",
    "记录ID",
    "市场",
    "操作",
    "股票代码",
    "股票名称",
    "成交日",
    "成交价格",
    "离场盈亏率",
    "投入份数",
    "行情来源",
    "证据SHA-256",
]
NAV_RUN_STATUS_PREFIX = "nav_run_status:"
SETTLEMENT_RUN_STATUS_PREFIX = "settlement_run_status:"
NAV_STATUS_LABELS = {
    "fresh": "本次运行已重估",
    "certified_carry_forward": "沿用最近认证快照",
    "unavailable": "不可估值",
    "status_record_missing": "状态记录缺失",
}


class ReportDataError(RuntimeError):
    """Raised when audited ledger data cannot support a truthful report."""


def strategy_display(strategy_id):
    strategy_id = str(strategy_id)
    return f"{STRAT_NAMES.get(strategy_id, strategy_id)} ({strategy_id})"



def load_active_strategy_accounts(db_path=None):
    import sqlite3
    from core.cash_manager import get_db_path

    conn = sqlite3.connect(db_path or get_db_path())
    try:
        account_filter, account_parameters, _ = quarantine_filter(
            conn, "strategy_accounts"
        )
        return conn.execute(
            "SELECT strategy_id, total_capital, available_cash "
            "FROM strategy_accounts WHERE 1=1"
            + account_filter
            + " ORDER BY strategy_id",
            account_parameters,
        ).fetchall()
    finally:
        conn.close()


def load_run_status(prefix, run_id, db_path=None):
    import sqlite3
    from core.cash_manager import get_db_path

    if not run_id:
        return None
    conn = sqlite3.connect(db_path or get_db_path())
    try:
        row = conn.execute(
            "SELECT value FROM meta_data WHERE key=?",
            (prefix + run_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            raise ValueError(f"Invalid {prefix} payload for run {run_id}")
        return payload
    finally:
        conn.close()


def _report_run_id():
    return os.environ.get("PIPELINE_RUN_ID") or os.environ.get("RUN_ID")


def _report_generated_at():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_md(value):
    return str(value or "-").replace("|", "\\|").replace("\n", " ")


def _nav_status_for(nav_status, strategy_id):
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


def render_cash_overview_md(accounts, nav_status):
    if not accounts:
        return ""
    md = "## 🏦 全球多策略子基金台账概览 (Sandbox Benchmark Engine)\n\n"
    md += (
        "| 策略沙盒 (Strategy) | 当前账本总资本 | 当前可用现金 | 认证 NAV | "
        "账本资金占用率 | NAV 状态 | 认证快照日期 | 市场估值日 | 说明 |\n"
    )
    md += "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    for sid, cap, cash in accounts:
        item = _nav_status_for(nav_status, sid)
        status = item.get("status", "status_record_missing")
        certified_nav = item.get("nav")
        certified_nav_text = (
            f"¥{float(certified_nav):,.2f}" if certified_nav is not None else "-"
        )
        util = ((cap - cash) / cap) * 100 if cap > 0 else 0
        md += (
            f"| `{sid}` | ¥{cap:,.2f} | ¥{cash:,.2f} | {certified_nav_text} | "
            f"{util:.1f}% | "
            f"{NAV_STATUS_LABELS.get(status, status)} | "
            f"{_safe_md(item.get('snapshot_date'))} | "
            f"{_safe_md(item.get('valuation_session'))} | "
            f"{_safe_md(item.get('failure_reason'))} |\n"
        )
    md += "\n> “沿用最近认证快照”不会写入伪造的当日 NAV；当前现金与快照现金可能因待交割或已成交指令而不同。\n\n---\n\n"
    return md


def render_cash_overview_html(accounts, nav_status):
    if not accounts:
        return ""
    result = (
        "<h2>🏦 全球多策略子基金台账概览 (Sandbox Benchmark Engine)</h2>\n"
        "<table id='nav-status-table'>\n<thead><tr>"
        "<th>策略沙盒 (Strategy)</th><th>当前账本总资本</th>"
        "<th>当前可用现金</th><th>认证 NAV</th><th>账本资金占用率</th><th>NAV 状态</th>"
        "<th>认证快照日期</th><th>市场估值日</th><th>说明</th>"
        "</tr></thead>\n<tbody>\n"
    )
    for sid, cap, cash in accounts:
        item = _nav_status_for(nav_status, sid)
        status = item.get("status", "status_record_missing")
        snapshot_date = str(item.get("snapshot_date") or "")
        valuation_session = str(item.get("valuation_session") or "")
        reason = str(item.get("failure_reason") or "-")
        certified_nav = item.get("nav")
        certified_nav_text = (
            f"¥{float(certified_nav):,.2f}" if certified_nav is not None else "-"
        )
        util = ((cap - cash) / cap) * 100 if cap > 0 else 0
        result += (
            "<tr "
            f"data-strategy='{html_lib.escape(str(sid), quote=True)}' "
            f"data-nav-status='{html_lib.escape(status, quote=True)}' "
            f"data-snapshot-date='{html_lib.escape(snapshot_date, quote=True)}' "
            f"data-valuation-session='{html_lib.escape(valuation_session, quote=True)}'>"
            f"<td><code>{html_lib.escape(str(sid))}</code></td>"
            f"<td>¥{cap:,.2f}</td><td>¥{cash:,.2f}</td>"
            f"<td>{html_lib.escape(certified_nav_text)}</td><td>{util:.1f}%</td>"
            f"<td>{html_lib.escape(NAV_STATUS_LABELS.get(status, status))}</td>"
            f"<td>{html_lib.escape(snapshot_date or '-')}</td>"
            f"<td>{html_lib.escape(valuation_session or '-')}</td>"
            f"<td>{html_lib.escape(reason)}</td></tr>\n"
        )
    result += (
        "</tbody></table>\n"
        "<div class='alert'>“沿用最近认证快照”不会写入伪造的当日 NAV；"
        "当前现金与快照现金可能因待交割或已成交指令而不同。</div>\n<hr>\n"
    )
    return result


def render_settlement_status_md(settlement_status):
    md = "## 🚦 本次交割状态\n\n"
    if not settlement_status:
        return md + "本次运行没有可验证的交割状态记录；报告不会据此声称已成交。\n\n"
    md += (
        "| 市场 | 发起策略 | 会话截止日 | 状态 | 本次买入 | 本次卖出 | "
        "当前待买 | 当前待卖 | 本次阻断 | 缺价延期 |\n"
    )
    md += "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    session = settlement_status.get("session_date") or "-"
    for market, item in sorted(settlement_status.get("markets", {}).items()):
        strategies = item.get("strategies") or {}
        if strategies:
            for strategy_id, detail in sorted(strategies.items()):
                filled = detail.get("filled_by_action", {})
                pending = detail.get("pending_by_action", {})
                md += (
                    f"| {market} | {_safe_md(strategy_display(strategy_id))} | "
                    f"{session} | {_safe_md(item.get('status'))} | "
                    f"{int(filled.get('BUY_NEW', 0)) + int(filled.get('ADD_TRANCHE', 0))} | "
                    f"{int(filled.get('SELL_ALL', 0))} | "
                    f"{int(pending.get('BUY_NEW', 0)) + int(pending.get('ADD_TRANCHE', 0))} | "
                    f"{int(pending.get('SELL_ALL', 0))} | "
                    f"{int(detail.get('blocked', 0))} | "
                    f"{int(detail.get('deferred', 0))} |\n"
                )
        else:
            md += (
                f"| {market} | 未提供策略拆分 | {session} | "
                f"{_safe_md(item.get('status'))} | 未拆分（合计 {int(item.get('filled', 0))}） | "
                f"未拆分 | 未拆分（合计 {int(item.get('pending', 0))}） | 未拆分 | "
                f"{int(item.get('blocked', 0))} | "
                f"{len(item.get('deferred', []))} |\n"
            )
    return md + "\n休市、尚未到合资格交易日或缺少权威开盘价只会延期交割，不阻止生成如实披露状态的正式报告。\n\n"


def render_settlement_status_html(settlement_status):
    result = "<h2>🚦 本次交割状态</h2>\n"
    if not settlement_status:
        return (
            result
            + "<p id='settlement-status' data-record-present='false'>"
            "本次运行没有可验证的交割状态记录；报告不会据此声称已成交。</p>\n"
        )
    session = str(settlement_status.get("session_date") or "")
    result += (
        "<table id='settlement-status-table' "
        f"data-session-date='{html_lib.escape(session, quote=True)}'>"
        "<thead><tr><th>市场</th><th>发起策略</th><th>会话截止日</th><th>状态</th>"
        "<th>本次买入</th><th>本次卖出</th><th>当前待买</th><th>当前待卖</th>"
        "<th>本次阻断</th><th>缺价延期</th></tr></thead><tbody>\n"
    )
    for market, item in sorted(settlement_status.get("markets", {}).items()):
        status = str(item.get("status") or "")
        strategies = item.get("strategies") or {}
        if strategies:
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
                result += (
                    f"<tr data-market='{html_lib.escape(market, quote=True)}' "
                    f"data-strategy='{html_lib.escape(str(strategy_id), quote=True)}' "
                    f"data-status='{html_lib.escape(status, quote=True)}'>"
                    f"<td>{html_lib.escape(market)}</td>"
                    f"<td>{html_lib.escape(strategy_display(strategy_id))}</td>"
                    f"<td>{html_lib.escape(session)}</td>"
                    f"<td>{html_lib.escape(status)}</td><td>{filled_buy}</td>"
                    f"<td>{filled_sell}</td><td>{pending_buy}</td>"
                    f"<td>{pending_sell}</td><td>{blocked}</td>"
                    f"<td>{deferred}</td></tr>\n"
                )
        else:
            result += (
                f"<tr data-market='{html_lib.escape(market, quote=True)}' "
                "data-strategy='' "
                f"data-status='{html_lib.escape(status, quote=True)}'>"
                f"<td>{html_lib.escape(market)}</td><td>未提供策略拆分</td>"
                f"<td>{html_lib.escape(session)}</td><td>{html_lib.escape(status)}</td>"
                f"<td>未拆分（合计 {int(item.get('filled', 0))}）</td>"
                "<td>未拆分</td>"
                f"<td>未拆分（合计 {int(item.get('pending', 0))}）</td>"
                "<td>未拆分</td>"
                f"<td>{int(item.get('blocked', 0))}</td>"
                f"<td>{len(item.get('deferred', []))}</td></tr>\n"
            )
    result += (
        "</tbody></table>\n<div class='alert'>休市、尚未到合资格交易日或缺少权威"
        "开盘价只会延期交割，不阻止生成如实披露状态的正式报告。</div>\n"
    )
    return result


def load_execution_ledger_summary(db_path=None):
    """Return v6/v7 execution counts without conflating legacy trades."""
    import sqlite3
    from core.cash_manager import get_db_path

    conn = sqlite3.connect(db_path or get_db_path())
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"orders", "fills"}.issubset(tables):
            return {"available": False, "orders": {}, "fills": {}, "intents": {}}
        orders = dict(
            conn.execute(
                "SELECT strategy_id, COUNT(*) FROM orders GROUP BY strategy_id"
            )
        )
        fills = dict(
            conn.execute(
                "SELECT o.strategy_id, COUNT(*) FROM fills f "
                "JOIN orders o ON o.order_id=f.order_id GROUP BY o.strategy_id"
            )
        )
        intents = {}
        if "trade_intents" in tables:
            supersession_filter = (
                " WHERE NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
                "WHERE s.intent_id=trade_intents.intent_id)"
                if "trade_intent_supersessions" in tables
                else ""
            )
            intents = {
                strategy: {"pending": pending, "filled": filled}
                for strategy, pending, filled in conn.execute(
                    "SELECT strategy_id,"
                    "SUM(CASE WHEN state='PENDING' THEN 1 ELSE 0 END),"
                    "SUM(CASE WHEN state='FILLED' THEN 1 ELSE 0 END) "
                    "FROM trade_intents"
                    + supersession_filter
                    + " GROUP BY strategy_id"
                )
            }
        return {
            "available": True,
            "v7_available": "trade_intents" in tables,
            "orders": orders,
            "fills": fills,
            "intents": intents,
        }
    finally:
        conn.close()


def build_actual_position_rows(positions, candidates, code_map=None):
    """Render only persisted portfolio rows as holdings; candidates are enrichment only."""
    code_map = code_map or {}
    candidate_by_code = {
        str(item.get("股票代码")): item
        for item in candidates
        if item.get("股票代码") is not None
    }
    rows = []
    for code in sorted(positions):
        position = positions[code]
        candidate = candidate_by_code.get(str(code), {})
        name = candidate.get("股票简称") or code_map.get(str(code)) or str(code)
        rows.append(
            {
                "股票代码": str(code),
                "股票简称": str(name),
                "买入日期": position.get("entry_date", ""),
                "买入价格": position.get("entry_price", 0),
                "仓位份数": position.get("shares", 1),
            }
        )
    return rows


def execution_ledger_text(strategy_id, summary):
    if not summary or not summary.get("available"):
        return "v6 执行账本尚不可用；以下 legacy 平仓记录不能视为券商成交确认。"
    orders = int(summary.get("orders", {}).get(strategy_id, 0))
    fills = int(summary.get("fills", {}).get(strategy_id, 0))
    text = (
        f"v6 执行账本：{orders} 张订单、{fills} 笔成交。"
        "legacy trade_history 仅为内部策略账本，不等同于券商交割单。"
    )
    if summary.get("v7_available"):
        intents = summary.get("intents", {}).get(strategy_id, {})
        text += (
            f" v7 交割意图：{int(intents.get('pending', 0))} 笔待交割、"
            f"{int(intents.get('filled', 0))} 笔已按开盘价结算。"
        )
    return text


def load_pending_trade_intents(db_path=None, code_map=None):
    import sqlite3
    from core.cash_manager import get_db_path

    code_map = code_map or {}
    conn = sqlite3.connect(db_path or get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_intents'"
        ).fetchone()
        if table is None:
            return None
        has_supersessions = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='trade_intent_supersessions'"
        ).fetchone()
        supersession_filter = (
            " AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
            "WHERE s.intent_id=trade_intents.intent_id)"
            if has_supersessions
            else ""
        )
        result = {strategy: [] for strategy in STRAT_NAMES}
        for row in conn.execute(
            "SELECT intent_id,source_run_id,signal_date,strategy_id,symbol,market,"
            "action,state,eligible_session,reason "
            "FROM trade_intents WHERE state='PENDING' "
            + supersession_filter
            + " "
            "ORDER BY strategy_id,CASE action WHEN 'SELL_ALL' THEN 0 ELSE 1 END,"
            "COALESCE(target_rank,0),symbol"
        ):
            symbol = str(row["symbol"])
            name = code_map.get(symbol) or code_map.get(str(symbol))
            if not name or name == symbol:
                name = get_stock_name(symbol)
            result.setdefault(row["strategy_id"], []).append(
                {
                    "发起策略": strategy_display(row["strategy_id"]),
                    "意图ID": str(row["intent_id"]),
                    "市场": str(row["market"]),
                    "操作": {"SELL_ALL": "卖出", "BUY_NEW": "买入", "ADD_TRANCHE": "加仓"}.get(
                        row["action"], row["action"]
                    ),
                    "股票代码": symbol,
                    "股票名称": name,
                    "信号日": str(row["signal_date"]),
                    "状态": "待交割",
                    "最早交割日": row["eligible_session"],
                    "说明": (
                        f"{row['reason'] or ''}；来源运行={row['source_run_id']}"
                    ).strip("；"),
                }
            )
        return result
    finally:
        conn.close()


def load_filled_trade_intents(db_path=None, code_map=None):
    """Load active v7 fills and require one valid v8 evidence row per fill."""
    import sqlite3
    from core.cash_manager import get_db_path

    code_map = code_map or {}
    conn = sqlite3.connect(db_path or get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "trade_intents" not in tables:
            return None
        has_supersessions = "trade_intent_supersessions" in tables
        supersession_filter = (
            " AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
            "WHERE s.intent_id=i.intent_id)"
            if has_supersessions
            else ""
        )
        active_filled = conn.execute(
            "SELECT COUNT(*) FROM trade_intents i "
            "WHERE i.state='FILLED'" + supersession_filter
        ).fetchone()[0]
        if active_filled and "trade_execution_evidence" not in tables:
            raise ReportDataError(
                "Active FILLED trade intents exist without the v8 execution "
                "evidence table"
            )

        result = {strategy: [] for strategy in STRAT_NAMES}
        if not active_filled:
            return result

        rows = conn.execute(
            "SELECT i.intent_id,i.strategy_id,i.symbol,i.market,i.action,"
            "i.tranche_quantity,i.eligible_session,i.execution_price,"
            "i.executed_at,e.execution_session,e.price_field,e.adjustment,"
            "e.provider,e.observed_at,e.payload_sha256 "
            "FROM trade_intents i "
            "LEFT JOIN trade_execution_evidence e ON e.intent_id=i.intent_id "
            "WHERE i.state='FILLED'"
            + supersession_filter
            + " ORDER BY i.strategy_id,e.execution_session,i.executed_at,"
            "i.intent_id"
        ).fetchall()
        if len(rows) != active_filled:
            raise ReportDataError(
                "Active FILLED trade-intent count changed while building the report"
            )

        for row in rows:
            symbol = str(row["symbol"])
            missing_evidence = any(
                row[field] is None
                for field in (
                    "execution_session",
                    "price_field",
                    "adjustment",
                    "provider",
                    "observed_at",
                    "payload_sha256",
                )
            )
            if missing_evidence:
                raise ReportDataError(
                    f"{row['strategy_id']}/{symbol} FILLED intent "
                    f"{row['intent_id']} is missing execution evidence"
                )
            if (
                row["execution_price"] is None
                or float(row["execution_price"]) <= 0
            ):
                raise ReportDataError(
                    f"{row['strategy_id']}/{symbol} FILLED intent has no "
                    "positive execution price"
                )
            if row["execution_session"] != row["eligible_session"]:
                raise ReportDataError(
                    f"{row['strategy_id']}/{symbol} execution session does not "
                    "match its eligible session"
                )
            if row["price_field"] != "open" or row["adjustment"] != "raw":
                raise ReportDataError(
                    f"{row['strategy_id']}/{symbol} execution evidence is not "
                    "an unadjusted open price"
                )
            payload_sha256 = str(row["payload_sha256"])
            if not re.fullmatch(r"[0-9a-fA-F]{64}", payload_sha256):
                raise ReportDataError(
                    f"{row['strategy_id']}/{symbol} execution evidence has an "
                    "invalid payload SHA-256"
                )
            name = code_map.get(symbol) or get_stock_name(symbol)
            result.setdefault(row["strategy_id"], []).append(
                {
                    "发起策略": strategy_display(row["strategy_id"]),
                    "意图ID": str(row["intent_id"]),
                    "市场": str(row["market"]),
                    "操作": {
                        "SELL_ALL": "卖出",
                        "BUY_NEW": "买入",
                        "ADD_TRANCHE": "加仓",
                    }.get(row["action"], row["action"]),
                    "股票代码": symbol,
                    "股票名称": name or symbol,
                    "状态": "已按开盘价结算",
                    "成交日": str(row["execution_session"]),
                    "成交价格": f"{float(row['execution_price']):.2f}",
                    "投入份数": str(int(row["tranche_quantity"])),
                    "价格字段": str(row["price_field"]),
                    "复权口径": str(row["adjustment"]),
                    "行情来源": str(row["provider"]),
                    "证据SHA-256": payload_sha256.lower(),
                }
            )
        return result
    finally:
        conn.close()


def load_unified_trade_records(db_path=None, code_map=None):
    """Load an auditable, deduplicated execution and legacy-history view."""
    import sqlite3
    from core.cash_manager import get_db_path
    from db_utils import test_strategy_filter

    code_map = code_map or {}
    conn = sqlite3.connect(db_path or get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "trade_intents" not in tables and "trade_history" not in tables:
            return None

        has_supersessions = "trade_intent_supersessions" in tables
        supersession_filter = (
            " AND NOT EXISTS (SELECT 1 FROM trade_intent_supersessions s "
            "WHERE s.intent_id=i.intent_id)"
            if has_supersessions
            else ""
        )

        active_filled = 0
        filled_rows = []
        if "trade_intents" in tables:
            intent_test_filter, intent_test_parameters = test_strategy_filter(
                "strategy_id"
            )
            active_filled = conn.execute(
                "SELECT COUNT(*) FROM trade_intents i WHERE i.state='FILLED'"
                + supersession_filter
                + intent_test_filter,
                intent_test_parameters,
            ).fetchone()[0]
        if active_filled and "trade_execution_evidence" not in tables:
            raise ReportDataError(
                "Active FILLED trade intents exist without the v8 execution "
                "evidence table"
            )
        if active_filled:
            filled_rows = conn.execute(
                "SELECT i.intent_id, i.strategy_id, i.symbol, i.market, i.action, "
                "i.tranche_quantity, i.eligible_session, i.execution_price, "
                "i.executed_at, e.execution_session, e.price_field, e.adjustment, "
                "e.provider, e.observed_at, e.payload_sha256, e.payload_json "
                "FROM trade_intents i "
                "LEFT JOIN trade_execution_evidence e ON e.intent_id=i.intent_id "
                "WHERE i.state='FILLED'"
                + supersession_filter
                + intent_test_filter
                + " ORDER BY i.strategy_id, e.execution_session, i.executed_at, i.intent_id"
                ,
                intent_test_parameters,
            ).fetchall()
            if len(filled_rows) != active_filled:
                raise ReportDataError(
                    "Active FILLED trade-intent count changed while building the report"
                )

        th_rows = []
        if "trade_history" in tables:
            trade_filter, trade_parameters, _ = quarantine_filter(
                conn, "trade_history"
            )
            trade_test_filter, trade_test_parameters = test_strategy_filter("strategy")
            th_rows = conn.execute(
                "SELECT id,strategy,name_or_code,entry_date,entry_price,exit_date,"
                "exit_price,pnl,reason,shares FROM trade_history WHERE 1=1"
                + trade_filter
                + trade_test_filter
                + " ORDER BY id",
                trade_parameters + trade_test_parameters,
            ).fetchall()

        history_by_intent = {}
        for th in th_rows:
            match = re.search(r"\[INTENT:([^\]]+)\]", str(th["reason"] or ""))
            if not match:
                continue
            intent_id = match.group(1)
            if intent_id in history_by_intent:
                raise ReportDataError(
                    f"Multiple trade_history rows claim intent {intent_id}"
                )
            history_by_intent[intent_id] = th

        result = {strategy: [] for strategy in STRAT_NAMES}
        consumed_history_ids = set()

        for row in filled_rows:
            symbol = str(row["symbol"])
            strat = str(row["strategy_id"])
            if strat not in result:
                continue
            action_raw = row["action"]
            action_label = {
                "SELL_ALL": "卖出",
                "BUY_NEW": "买入",
                "ADD_TRANCHE": "加仓",
            }.get(action_raw, action_raw)

            missing_evidence = any(
                row[field] is None
                for field in (
                    "execution_session",
                    "price_field",
                    "adjustment",
                    "provider",
                    "observed_at",
                    "payload_sha256",
                    "payload_json",
                )
            )
            if missing_evidence:
                raise ReportDataError(
                    f"{strat}/{symbol} FILLED intent {row['intent_id']} "
                    "is missing execution evidence"
                )
            if row["execution_price"] is None or float(row["execution_price"]) <= 0:
                raise ReportDataError(
                    f"{strat}/{symbol} FILLED intent has no positive execution price"
                )
            if row["execution_session"] != row["eligible_session"]:
                raise ReportDataError(
                    f"{strat}/{symbol} execution session does not match eligible session"
                )
            if row["price_field"] != "open" or row["adjustment"] != "raw":
                raise ReportDataError(
                    f"{strat}/{symbol} execution evidence is not an unadjusted open"
                )
            payload_sha256 = str(row["payload_sha256"]).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", payload_sha256):
                raise ReportDataError(
                    f"{strat}/{symbol} execution evidence has an invalid SHA-256"
                )
            actual_sha256 = hashlib.sha256(
                str(row["payload_json"]).encode("utf-8")
            ).hexdigest()
            if actual_sha256 != payload_sha256:
                raise ReportDataError(
                    f"{strat}/{symbol} execution evidence SHA-256 mismatch"
                )
            name = code_map.get(symbol) or get_stock_name(symbol)
            session = str(row["execution_session"])

            pnl_str = "-"
            if action_raw == "SELL_ALL":
                th_matched = history_by_intent.get(str(row["intent_id"]))
                if th_matched is None:
                    raise ReportDataError(
                        f"FILLED SELL_ALL intent {row['intent_id']} has no exact "
                        "trade_history link"
                    )
                if (
                    str(th_matched["strategy"]) != strat
                    or str(th_matched["name_or_code"]) != symbol
                    or str(th_matched["exit_date"]) != session
                    or abs(
                        float(th_matched["exit_price"])
                        - float(row["execution_price"])
                    )
                    >= 0.01
                ):
                    raise ReportDataError(
                        f"FILLED SELL_ALL intent {row['intent_id']} does not match "
                        "its linked trade_history row"
                    )
                consumed_history_ids.add(int(th_matched["id"]))
                if th_matched["pnl"] is not None:
                    pnl_str = f"{float(th_matched['pnl']) * 100:+.2f}%"

            result.setdefault(strat, []).append({
                "发起策略": strategy_display(strat),
                "记录ID": str(row["intent_id"]),
                "市场": str(row["market"]),
                "操作": action_label,
                "股票代码": symbol,
                "股票名称": name or symbol,
                "成交日": session,
                "成交价格": f"{float(row['execution_price']):.2f}",
                "离场盈亏率": pnl_str,
                "投入份数": str(int(row["tranche_quantity"])),
                "行情来源": str(row["provider"]),
                "证据SHA-256": payload_sha256,
            })

        market_by_strategy = {
            strategy: (
                "A"
                if strategy.endswith("_a_stock")
                else "HK"
                if strategy.endswith("_hk_stock")
                else "US"
            )
            for strategy in STRAT_NAMES
        }
        for th in th_rows:
            if int(th["id"]) in consumed_history_ids:
                continue
            strat = str(th["strategy"])
            if strat not in result:
                continue
            symbol = str(th["name_or_code"])
            name = code_map.get(symbol) or get_stock_name(symbol)
            pnl_val = float(th["pnl"]) * 100 if th["pnl"] is not None else 0.0
            pnl_str = f"{pnl_val:+.2f}%"

            result.setdefault(strat, []).append({
                "发起策略": strategy_display(strat),
                "记录ID": f"legacy-trade:{int(th['id'])}",
                "市场": market_by_strategy[strat],
                "操作": "卖出平仓",
                "股票代码": symbol,
                "股票名称": name or symbol,
                "成交日": str(th["exit_date"]),
                "成交价格": (
                    f"{float(th['exit_price']):.2f}"
                    if th["exit_price"] is not None and float(th["exit_price"]) > 0
                    else "N/A"
                ),
                "离场盈亏率": pnl_str,
                "投入份数": str(int(th["shares"] or 1)),
                "行情来源": "Legacy",
                "证据SHA-256": "无（历史账本未保存原始行情摘要）",
            })

        for strat in result:
            result[strat].sort(
                key=lambda row: (row["成交日"], row["记录ID"]), reverse=True
            )

        return result
    finally:
        conn.close()


def render_table_md(items, headers):
    if len(items) == 0:
        return "暂无符合条件的标的。\n\n"

    res = "| " + " | ".join(headers) + " |\n"
    res += "|" + "|".join(["---"] * len(headers)) + "|\n"
    for row in items:
        cells = []
        for h in headers:
            val = row.get(h)
            if val is None:
                cells.append("")
            elif isinstance(val, (float, int)):
                if h == "入选价格" and val <= 0:
                    cells.append("等待开盘")
                else:
                    cells.append(f"{float(val):.2f}")
            else:
                cells.append(str(val))
        res += "| " + " | ".join(cells) + " |\n"
    return res + "\n\n"

def render_table_html(items, headers):
    if len(items) == 0:
        return "<p>暂无符合条件的标的。</p>\n"

    res = "<table>\n"
    res += "  <thead>\n    <tr>\n"
    for h in headers:
        res += f"      <th class='nowrap'>{h}</th>\n"
    res += "    </tr>\n  </thead>\n  <tbody>\n"

    for row in items:
        res += "    <tr>\n"
        for h in headers:
            val = row.get(h)
            if val is None:
                cell = ""
            elif isinstance(val, (float, int)):
                if h == "入选价格" and val <= 0:
                    cell = "等待开盘"
                else:
                    cell = f"{float(val):.2f}"
            else:
                cell = str(val)

            if h == "累计涨跌幅":
                if cell.startswith("-"):
                    cell = f"<span class='loss'>{cell}</span>"
                elif cell != "0.00%" and cell != "":
                    cell = f"<span class='win'>+{cell}</span>"
            if h in ["股票代码", "股票简称", "买入日期", "卖出日期", "最新价", "入选日期", "入选价格", "累计涨跌幅"]:
                res += f"      <td class='nowrap'>{cell}</td>\n"
            else:
                res += f"      <td>{cell}</td>\n"
        res += "    </tr>\n"
    res += "  </tbody>\n</table>\n"
    return res

def render_history_md(strategy_id, trade_history, code_map=None):
    if code_map is None:
        code_map = {}
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    if not strat_trades:
        return "暂无内部账本平仓记录。\n\n"

    res = f"共 {len(strat_trades)} 笔内部账本平仓记录。\n\n"
    res += "| 股票代码/简称 | 买入日期 | 均价 | 投入份数 | 卖出日期 | 卖出价格 | 最终盈亏率 | 账本备注 |\n"
    res += "|---|---|---|---|---|---|---|---|\n"
    for trade in reversed(strat_trades):
        code = trade.get("name", "")
        name = code_map.get(str(code), code)
        if name == code:
            name = get_stock_name(code)

        if name != code:
            display_name = f"{code} ({name})"
        else:
            display_name = code

        in_d = trade.get("entry_date", "")
        in_p = trade.get("entry_price", 0)
        shares = trade.get("shares", 1)
        out_d = trade.get("exit_date", "")
        out_p = trade.get("exit_price", 0)
        pnl = trade.get("pnl", 0) * 100

        in_p_str = f"{in_p:.2f}" if in_p > 0 else "等待开盘"
        out_p_str = f"{out_p:.2f}" if out_p > 0 else "等待开盘"

        if in_p <= 0 or out_p <= 0:
            pnl_str = "N/A"
        else:
            pnl_str = f"<span style='color:red'>+{pnl:.2f}%</span>" if pnl > 0 else f"<span style='color:green'>{pnl:.2f}%</span>"

        reason = trade.get("reason")
        if reason is None or str(reason).strip() == "" or str(reason).strip().lower() == "none":
            reason = "-"

        res += f"| {display_name} | {in_d} | {in_p_str} | {shares} | {out_d} | {out_p_str} | {pnl_str} | {reason} |\n"
    return res + "\n\n"

def render_history_html(strategy_id, trade_history, code_map=None):
    if code_map is None:
        code_map = {}
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    if not strat_trades:
        return "<p>暂无内部账本平仓记录。</p>\n"

    res = f"<p>共 {len(strat_trades)} 笔内部账本平仓记录。</p>\n"
    res += "<table>\n  <thead>\n    <tr>\n"
    for h in ["股票代码/简称", "买入日期", "均价", "投入份数", "卖出日期", "卖出价格", "最终盈亏率", "账本备注"]:
        res += f"      <th class='nowrap'>{h}</th>\n"
    res += "    </tr>\n  </thead>\n  <tbody>\n"

    for trade in reversed(strat_trades):
        code = trade.get("name", "")
        name = code_map.get(str(code), code)
        if name == code:
            name = get_stock_name(code)

        if name != code:
            display_name = f"{code} ({name})"
        else:
            display_name = code

        in_d = trade.get("entry_date", "")
        in_p = trade.get("entry_price", 0)
        shares = trade.get("shares", 1)
        out_d = trade.get("exit_date", "")
        out_p = trade.get("exit_price", 0)
        pnl = trade.get("pnl", 0) * 100

        in_p_str = f"{in_p:.2f}" if in_p > 0 else "等待开盘"
        out_p_str = f"{out_p:.2f}" if out_p > 0 else "等待开盘"

        if in_p <= 0 or out_p <= 0:
            pnl_str = "N/A"
        else:
            pnl_cls = "win" if pnl > 0 else "loss" if pnl < 0 else ""
            pnl_sign = "+" if pnl > 0 else ""
            pnl_str = f"<span class='{pnl_cls}'>{pnl_sign}{pnl:.2f}%</span>"

        reason = trade.get("reason")
        if reason is None or str(reason).strip() == "" or str(reason).strip().lower() == "none":
            reason = "-"

        res += f"    <tr>\n"
        res += f"      <td class='nowrap' style='text-align:left'>{display_name}</td>\n"
        res += f"      <td class='nowrap'>{in_d}</td>\n"
        res += f"      <td class='nowrap'>{in_p_str}</td>\n"
        res += f"      <td class='nowrap'>{shares}</td>\n"
        res += f"      <td class='nowrap'>{out_d}</td>\n"
        res += f"      <td class='nowrap'>{out_p_str}</td>\n"
        res += f"      <td class='nowrap'>{pnl_str}</td>\n"
        res += f"      <td>{reason}</td>\n"
        res += f"    </tr>\n"
    res += "  </tbody>\n</table>\n"
    return res

def get_chart_md(chart_name, base_dir):
    chart_path = os.path.join(base_dir, "reports", chart_name)
    if not os.path.exists(chart_path):
        chart_path = os.path.join(base_dir, chart_name)
    if os.path.exists(chart_path):
        return f"![{chart_name}]({chart_path})\n\n"
    return ""

def get_chart_html(chart_name, base_dir):
    chart_path = os.path.join(base_dir, "reports", chart_name)
    if not os.path.exists(chart_path):
        chart_path = os.path.join(base_dir, chart_name)

    if os.path.exists(chart_path):
        with open(chart_path, "rb") as img:
            b64 = base64.b64encode(img.read()).decode("utf-8")
            return f"<div class='chart-container'><img src='data:image/png;base64,{b64}' alt='{chart_name}'></div>\n"
    return ""

def generate_batch_llm_reviews(strategies_dict):
    if not strategies_dict or not call_llm:
        return {}

    # Slim down payload to save tokens
    slim_payload = {}
    for strat, items in strategies_dict.items():
        if not items: continue
        slim_items = []
        for item in items[:10]: # Only top 10
            slim_items.append({
                "代码": item.get("股票代码", ""),
                "简称": item.get("股票简称", ""),
                "行业": item.get("所属行业", ""),
                "市值": item.get("总市值", item.get("总市值(元)", "")),
                "PE": item.get("PE", item.get("市盈率(TTM)", "")),
                "最新价": item.get("最新价", "")
            })
        if slim_items:
            slim_payload[strat] = slim_items

    if not slim_payload:
        return {}

    prompt = f"""作为资深量化基金经理，以下是各大子策略今日选出的 Top 10 股票核心指标：
{json.dumps(slim_payload, ensure_ascii=False)}

请结合基本面常识，为每个策略分别给出质性评价。请严格以 JSON 格式返回，结构如下：
{{
  "strategy_reviews": {{
    "strategy_name": {{
      "reviews": [
        {{
          "股票代码": "代码",
          "股票简称": "简称",
          "护城河打分": 3.5,
          "成长性打分": 4.2,
          "一句话点评": "极短点评内容"
        }}
      ],
      "summary": "该策略总结"
    }}
  }}
}}
"""

    import time
    max_retries = 3
    base_delay = 5

    for attempt in range(max_retries):
        print(f"Generating LLM batch reviews (Attempt {attempt+1}/{max_retries})...", flush=True)
        try:
            res = call_llm(prompt, require_json=True)
            if res and isinstance(res, dict) and "strategy_reviews" in res:
                html_outputs = {}
                for strat, strat_data in res["strategy_reviews"].items():
                    reviews = strat_data.get("reviews", [])
                    for r in reviews:
                        r["合计分"] = float(r.get("护城河打分", 0)) + float(r.get("成长性打分", 0))
                    reviews.sort(key=lambda x: x.get("合计分", 0), reverse=True)

                    html = "<div class='llm-review'>\n<h3>🤖 AI 质性点评与打分</h3>\n"
                    html += "<table>\n  <thead>\n    <tr>\n      <th>股票代码</th><th>股票简称</th><th>护城河打分(1-5)</th><th>成长性打分(1-5)</th><th>合计分(满分10)</th><th>一句话点评</th>\n    </tr>\n  </thead>\n  <tbody>\n"
                    for r in reviews:
                        html += f"    <tr>\n      <td>{r.get('股票代码','')}</td><td>{r.get('股票简称','')}</td><td>{r.get('护城河打分','')}</td><td>{r.get('成长性打分','')}</td><td>{r.get('合计分',0):.1f}</td><td>{r.get('一句话点评','')}</td>\n    </tr>\n"
                    html += "  </tbody>\n</table>\n"
                    if strat_data.get("summary"):
                        html += f"<p><strong>总评：</strong>{strat_data['summary']}</p>\n"
                    html += "</div>\n"
                    html_outputs[strat] = html
                return html_outputs
            else:
                print(f"LLM returned invalid json for batch: {res}")
        except Exception as e:
            print(f"Failed to generate LLM batch reviews: {e}")

        if attempt < max_retries - 1:
            time.sleep(base_delay * (2 ** attempt))

    return {}

def generate_subsection_md(
    strategy_id,
    results,
    headers,
    diff,
    trade_history,
    base_dir,
    llm_review="",
    code_map=None,
    appendix_results=None,
    positions=None,
    execution_summary=None,
    pending_intents=None,
    filled_intents=None,
    unified_trades=None,
):
    if appendix_results is None:
        appendix_results = []
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    title = STRAT_NAMES.get(strategy_id, strategy_id)
    actual_positions = build_actual_position_rows(
        positions or {}, results, code_map=code_map
    )
    title = STRAT_NAMES.get(strategy_id, strategy_id)
    out = f"### {title}\n\n"
    out += "**实际持仓（legacy portfolio）**\n\n"
    if actual_positions:
        out += render_table_md(actual_positions, POSITION_HEADERS)
    else:
        out += "暂无持仓。\n\n"

    out += "**本次筛选候选（研究池，未必已成交）**\n\n"
    if results:
        out += render_table_md(results, headers)
    else:
        out += "暂无符合条件的候选标的。\n\n"

    if appendix_results:
        out += "**备选池 (Appendix)**\n\n"
        out += render_table_md(appendix_results, headers)

    out += "**待交割指令（v7 trade_intents）**\n\n"
    if pending_intents is None:
        out += "v7 交割意图账本尚不可用。\n\n"
    elif pending_intents:
        out += render_table_md(pending_intents, INTENT_HEADERS)
    else:
        out += "暂无待交割指令。\n\n"

    out += "**交易执行与历史成交明细（v7 trade_intents + v8 evidence + legacy）**\n\n"
    if unified_trades is None and filled_intents is None:
        out += "v7/v8 成交证据账本尚不可用。\n\n"
    elif unified_trades is not None:
        u_list = unified_trades if isinstance(unified_trades, list) else unified_trades.get(strategy_id, [])
        if u_list:
            out += render_table_md(u_list, UNIFIED_TRADE_HEADERS)
        else:
            out += "暂无已成交或历史平仓记录。\n\n"
    elif filled_intents:
        out += render_table_md(filled_intents, FILLED_INTENT_HEADERS)
    else:
        out += "暂无有效的已交割成交记录。\n\n"

    strat_diff = diff.get(strategy_id, {})
    if strat_diff.get("added") or strat_diff.get("removed"):
        out += f"> **今日调仓提示**：\n"
        if strat_diff.get("added"):
            new_pool = []
            grid_adds = []
            for item in strat_diff["added"]:
                if isinstance(item, dict):
                    ep = item.get('entry_price', 0)
                    ep_str = f"{ep:.2f}" if ep > 0 else "等待开盘"
                    code = str(item['name'])
                    name = code_map.get(code, code) if code_map else code
                    if name == code:
                        name = get_stock_name(code)
                    display_name = f"{code} ({name})" if name != code else code

                    item_reason = item.get("reason", "")
                    if "网格加仓" in str(item_reason):
                        grid_adds.append(f"{display_name} (加仓价: {ep_str}, 原因: {item_reason})")
                    else:
                        strat_reason = STRAT_REASONS.get(strategy_id, "策略量化指标")
                        new_pool.append(f"{display_name} (入选价: {ep_str}, 原因: 满足【{strat_reason}】入选标准)")
                else:
                    new_pool.append(str(item))
            if new_pool:
                out += f"> 🟢 **新增入池**：{', '.join(new_pool)}\n"
            if grid_adds:
                out += f"> 🔵 **网格加仓**：{', '.join(grid_adds)}\n"
        if strat_diff.get("removed"):
            removed_strs = []
            for item in strat_diff["removed"]:
                if isinstance(item, dict):
                    ep = item.get("entry_price", 0)
                    cp = item.get("exit_price", 0)
                    pnl = item.get("pnl", 0) * 100

                    ep_str = f"{ep:.2f}" if ep > 0 else "等待开盘"
                    cp_str = f"{cp:.2f}" if cp > 0 else "等待开盘"
                    pnl_str = f"{pnl:.2f}%" if ep > 0 and cp > 0 else "N/A"

                    code = str(item['name'])
                    name = code_map.get(code, code) if code_map else code
                    if name == code:
                        name = get_stock_name(code)
                    display_name = f"{code} ({name})" if name != code else code

                    specific_reason = diagnose_elimination(code, strategy_id)
                    removed_strs.append(f"{display_name} (入选价: {ep_str}, 剔除价: {cp_str}, 盈亏: {pnl_str}, 原因: {specific_reason})")
                else:
                    removed_strs.append(str(item))
            out += f"> 🔴 **掉出观测**：{', '.join(removed_strs)}\n"
        out += "\n\n"

    if llm_review:
        out += llm_review

    out += f"> **账本口径**：{execution_ledger_text(strategy_id, execution_summary)}\n\n"
    if unified_trades is None:
        out += "**内部账本历史平仓记录（legacy trade_history）**\n\n"
        out += render_history_md(strategy_id, trade_history, code_map)

    out += "**资金净值曲线图**\n\n"
    out += get_chart_md(f"pnl_chart_{strategy_id}.png", base_dir)
    return out

def generate_subsection_html(
    strategy_id,
    results,
    headers,
    diff,
    trade_history,
    base_dir,
    llm_review="",
    code_map=None,
    appendix_results=None,
    positions=None,
    execution_summary=None,
    pending_intents=None,
    filled_intents=None,
    unified_trades=None,
):
    if appendix_results is None:
        appendix_results = []
    strat_trades = [t for t in trade_history if t.get("strategy") == strategy_id]
    title = STRAT_NAMES.get(strategy_id, strategy_id)
    actual_positions = build_actual_position_rows(
        positions or {}, results, code_map=code_map
    )
    title = STRAT_NAMES.get(strategy_id, strategy_id)
    html = f"<h3>{title}</h3>\n"
    html += "<h4>实际持仓（legacy portfolio）</h4>\n"
    if actual_positions:
        html += render_table_html(actual_positions, POSITION_HEADERS)
    else:
        html += "<p>暂无持仓。</p>\n"

    html += "<h4>本次筛选候选（研究池，未必已成交）</h4>\n"
    if results:
        html += render_table_html(results, headers)
    else:
        html += "<p>暂无符合条件的候选标的。</p>\n"

    if appendix_results:
        html += "<h4>备选池 (Appendix)</h4>\n"
        html += render_table_html(appendix_results, headers)

    html += "<h4>待交割指令（v7 trade_intents）</h4>\n"
    if pending_intents is None:
        html += "<p>v7 交割意图账本尚不可用。</p>\n"
    elif pending_intents:
        html += render_table_html(pending_intents, INTENT_HEADERS)
    else:
        html += "<p>暂无待交割指令。</p>\n"

    if unified_trades is not None:
        html += "<h4>交易执行与历史成交明细（v7 trade_intents + v8 evidence + legacy）</h4>\n"
        u_list = unified_trades if isinstance(unified_trades, list) else unified_trades.get(strategy_id, [])
        if u_list:
            html += render_table_html(u_list, UNIFIED_TRADE_HEADERS)
        else:
            html += "<p>暂无已成交或历史平仓记录。</p>\n"
    else:
        html += "<h4>已交割成交明细（v7 trade_intents + v8 evidence）</h4>\n"
        if filled_intents is None:
            html += "<p>v7/v8 成交证据账本尚不可用。</p>\n"
        elif filled_intents:
            html += render_table_html(filled_intents, FILLED_INTENT_HEADERS)
        else:
            html += "<p>暂无有效的已交割成交记录。</p>\n"

    strat_diff = diff.get(strategy_id, {})
    if strat_diff.get("added") or strat_diff.get("removed"):
        html += f"<div class='alert'>\n  <p><strong>今日调仓提示：</strong></p>\n"
        if strat_diff.get("added"):
            new_pool = []
            grid_adds = []
            for item in strat_diff["added"]:
                if isinstance(item, dict):
                    ep = item.get('entry_price', 0)
                    ep_str = f"{ep:.2f}" if ep > 0 else "等待开盘"
                    code = str(item['name'])
                    name = code_map.get(code, code) if code_map else code
                    if name == code:
                        name = get_stock_name(code)
                    display_name = f"{code} ({name})" if name != code else code

                    item_reason = item.get("reason", "")
                    if "网格加仓" in str(item_reason):
                        grid_adds.append(f"{display_name} (加仓价: {ep_str}, 原因: {item_reason})")
                    else:
                        strat_reason = STRAT_REASONS.get(strategy_id, "策略量化指标")
                        new_pool.append(f"{display_name} (入选价: {ep_str}, 原因: 满足【{strat_reason}】入选标准)")
                else:
                    new_pool.append(str(item))
            if new_pool:
                html += f"  <p>🟢 <strong>新增入池</strong>：{', '.join(new_pool)}</p>\n"
            if grid_adds:
                html += f"  <p>🔵 <strong>网格加仓</strong>：{', '.join(grid_adds)}</p>\n"
        if strat_diff.get("removed"):
            removed_strs = []
            for item in strat_diff["removed"]:
                if isinstance(item, dict):
                    ep = item.get("entry_price", 0)
                    cp = item.get("exit_price", 0)
                    pnl = item.get("pnl", 0) * 100

                    ep_str = f"{ep:.2f}" if ep > 0 else "等待开盘"
                    cp_str = f"{cp:.2f}" if cp > 0 else "等待开盘"
                    pnl_str = f"{pnl:.2f}%" if ep > 0 and cp > 0 else "N/A"
                    pnl_cls = "win" if pnl > 0 else "loss" if pnl < 0 else ""
                    pnl_sign = "+" if pnl > 0 else ""

                    code = str(item['name'])
                    name = code_map.get(code, code) if code_map else code
                    if name == code:
                        name = get_stock_name(code)
                    display_name = f"{code} ({name})" if name != code else code

                    specific_reason = diagnose_elimination(code, strategy_id)
                    removed_strs.append(f"{display_name} (入选价: {ep_str}, 剔除价: {cp_str}, <span class='{pnl_cls}'>盈亏: {pnl_sign}{pnl_str}</span>, 原因: {specific_reason})")
                else:
                    removed_strs.append(str(item))
            html += f"  <p>🔴 <strong>掉出观测</strong>：{', '.join(removed_strs)}</p>\n"
        html += "</div>\n"

    if llm_review:
        html += f"<div style='margin-top:20px; padding:15px; background-color:#eef2ff; border-radius:8px;'>{llm_review}</div>\n"
    html += (
        "<p class='ledger-note'><strong>账本口径：</strong>"
        f"{execution_ledger_text(strategy_id, execution_summary)}</p>\n"
    )
    if unified_trades is None:
        html += "<h4>内部账本历史平仓记录（legacy trade_history）</h4>\n"
        html += render_history_html(strategy_id, trade_history, code_map)
    html += "<h4>资金净值曲线图</h4>\n"
    html += get_chart_html(f"pnl_chart_{strategy_id}.png", base_dir)
    return html

def main():
    if len(sys.argv) < 3:
        print("Usage: python3 generate_report.py <input_json> <output_md>")
        sys.exit(1)

    input_file = sys.argv[1]
    output_md_file = sys.argv[2]
    base_dir = os.path.dirname(input_file)
    output_html_file = os.path.splitext(output_md_file)[0] + ".html"

    import db_utils

    portfolio, trade_history = db_utils.load_portfolio_and_trades()
    payload = db_utils.load_latest_daily_results()
    if not payload:
        print("No daily_results found in strategy_daily_results table")
        sys.exit(1)

    results = payload.get("results", {})
    diff = payload.get("diff", {})
    appendix = payload.get("appendix", {})
    run_id = _report_run_id()
    generated_at = _report_generated_at()
    nav_status = load_run_status(NAV_RUN_STATUS_PREFIX, run_id)
    settlement_status = load_run_status(SETTLEMENT_RUN_STATUS_PREFIX, run_id)
    execution_summary = load_execution_ledger_summary()
    code_map = {}
    for strat, items in results.items():
        for item in items:
            code = item.get("股票代码")
            name = item.get("股票简称")
            if code and name:
                code_map[str(code)] = str(name)

    try:
        from screen_a_share import load_code_name_table
        a_share_df = load_code_name_table()
        for _, row in a_share_df.iterrows():
            code_map[str(row["股票代码"])] = str(row["股票简称"])
    except Exception as e:
        print(f"Warning: Could not load A-share code map: {e}")

    pending_intents = load_pending_trade_intents(code_map=code_map)
    filled_intents = load_filled_trade_intents(code_map=code_map)
    unified_trades = load_unified_trade_records(code_map=code_map)

    snapshot_date = payload.get("snapshot_date", "1970-01-01")[:10]

    for strat, items in diff.items():
        if "added" in items:
            for item in items["added"]:
                if isinstance(item, dict):
                    code = str(item.get("name"))
                    if strat in portfolio and code in portfolio[strat]:
                        item["entry_price"] = portfolio[strat][code].get("entry_price", 0)
        if "removed" in items:
            for item in items["removed"]:
                if isinstance(item, dict):
                    code = str(item.get("name"))
                    for t in reversed(trade_history):
                        if t["strategy"] == strat and str(t["name"]) == code and str(t["exit_date"]).startswith(snapshot_date):
                            item["entry_price"] = t["entry_price"]
                            item["exit_price"] = t["exit_price"]
                            item["pnl"] = t["pnl"]
                            break

    for strat, items in results.items():
        if not items: continue
        for item in items:
            code = str(item.get("股票代码"))
            if strat in portfolio and code in portfolio[strat]:
                item["入选价格"] = portfolio[strat][code].get("entry_price", 0)
    # --- End Reconciliation ---

    for strat, items in results.items():
        if not items:
            continue
        if "dividend" in strat:
            items.sort(key=lambda x: float('inf') if x.get("估值公式值") is None else float(x.get("估值公式值")))
        elif "growth" in strat:
            items.sort(key=lambda x: -float('inf') if x.get("净资产收益率") is None else float(x.get("净资产收益率")), reverse=True)
        elif "hot_spot" in strat:
            pass # Keep original sorting which is by turnover

    div_headers = ["股票代码", "股票简称", "PE", "PB", "估值公式值", "TTM股息率", "3年净利润CAGR", "入选日期", "入选价格", "仓位份数", "累计涨跌幅"]
    gro_headers = ["股票代码", "股票简称", "PE", "净资产收益率", "营业总收入同比增长率", "净利润同比增长率", "入选日期", "入选价格", "仓位份数", "累计涨跌幅"]
    hot_headers = ["股票代码", "股票简称", "最新价", "涨跌幅(%)", "成交额(亿)", "入选日期", "入选价格", "仓位份数", "累计涨跌幅", "入选理由"]

    # Pre-generate LLM batch review for all strategies at once to save tokens and threads
    llm_reviews = {}
    if call_llm and os.environ.get("PIPELINE_DISABLE_LLM") != "1":
        strategies_to_review = [
            "dividend_a_stock", "growth_a_stock", "growth_us_stock", "growth_hk_stock",
            "hot_spot_a_stock", "hot_spot_us_stock", "hot_spot_hk_stock"
        ]

        batch_input = {}
        for strat in strategies_to_review:
            if results.get(strat):
                batch_input[strat] = results[strat]

        if batch_input:
            llm_reviews = generate_batch_llm_reviews(batch_input)


    def subsection_md(strategy_id, headers):
        return generate_subsection_md(
            strategy_id,
            results.get(strategy_id, []),
            headers,
            diff,
            trade_history,
            base_dir,
            llm_reviews.get(strategy_id, ""),
            code_map,
            appendix.get(strategy_id, []),
            positions=portfolio.get(strategy_id, {}),
            execution_summary=execution_summary,
            pending_intents=(
                None if pending_intents is None else pending_intents.get(strategy_id, [])
            ),
            filled_intents=(
                None if filled_intents is None else filled_intents.get(strategy_id, [])
            ),
            unified_trades=(
                None if unified_trades is None else unified_trades.get(strategy_id, [])
            ),
        )

    def subsection_html(strategy_id, headers):
        return generate_subsection_html(
            strategy_id,
            results.get(strategy_id, []),
            headers,
            diff,
            trade_history,
            base_dir,
            llm_reviews.get(strategy_id, ""),
            code_map,
            appendix.get(strategy_id, []),
            positions=portfolio.get(strategy_id, {}),
            execution_summary=execution_summary,
            pending_intents=(
                None if pending_intents is None else pending_intents.get(strategy_id, [])
            ),
            filled_intents=(
                None if filled_intents is None else filled_intents.get(strategy_id, [])
            ),
            unified_trades=(
                None if unified_trades is None else unified_trades.get(strategy_id, [])
            ),
        )

    # ================= MARKDOWN GENERATION =================
    accounts = load_active_strategy_accounts()
    out = "# 每日全球策略量化报告\n\n"
    out += (
        f"> 运行 ID：`{_safe_md(run_id or 'unbound')}`  \n"
        f"> 信号日期：`{_safe_md(snapshot_date)}`  \n"
        f"> 报告生成时间（UTC）：`{_safe_md(generated_at)}`\n\n"
    )
    out += render_cash_overview_md(accounts, nav_status)
    out += render_settlement_status_md(settlement_status)

    out += "## 🟢 第一章：稳健红利策略 (基本面护城河)\n\n"
    out += subsection_md("dividend_a_stock", div_headers)

    out += "---\n\n## 🔵 第二章：高增成长策略 (基本面护城河)\n\n"
    out += subsection_md("growth_a_stock", gro_headers)
    out += subsection_md("growth_us_stock", gro_headers)
    out += subsection_md("growth_hk_stock", gro_headers)

    out += "---\n\n## 🔴 第三章：产业热点战法 (AI 宏观洞察与事件驱动)\n\n"
    out += subsection_md("hot_spot_a_stock", hot_headers)
    out += subsection_md("hot_spot_us_stock", hot_headers)
    out += subsection_md("hot_spot_hk_stock", hot_headers)

    out += "---\n\n## 🌟 四、全策略综合对比总结 (Master Chart)\n\n"
    if os.path.exists(os.path.join(base_dir, "nav_chart_all.png")):
        out += get_chart_md("nav_chart_all.png", base_dir)
    out += get_chart_md("pnl_chart_all.png", base_dir)

    os.makedirs(os.path.dirname(output_md_file), exist_ok=True)
    with open(output_md_file, "w", encoding="utf-8") as f:
        f.write(out)

    # ================= HTML GENERATION =================
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>每日全球策略量化报告</title>
    <style>
        :root { --bg: #f9fafb; --card: #ffffff; --text: #1f2937; --border: #e5e7eb; --red: #dc2626; --green: #16a34a; }
        body { font-family: -apple-system, sans-serif; background: var(--bg); color: var(--text); padding: 10px; }
        .container { max-width: 98%; margin: 0 auto; background: var(--card); padding: 20px; border-radius: 12px; }
        h1 { text-align: center; color: #4f46e5; }
        h2 { border-bottom: 2px solid var(--border); padding-bottom: 10px; margin-top: 40px; }
        h3 { color: #4b5563; border-left: 4px solid #4f46e5; padding-left: 10px; }
        h4 { margin-top: 20px; }
        table { width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 13px; }
        th, td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
        td:last-child, th:last-child { white-space: normal; min-width: 150px; max-width: 400px; }
        .nowrap { white-space: nowrap; }
        th { background: #f3f4f6; position: sticky; top: 0; }
        th:nth-child(1), td:nth-child(1), th:nth-child(2), td:nth-child(2) { text-align: left; }
        .win { color: var(--red); font-weight: bold; }
        .loss { color: var(--green); font-weight: bold; }
        .alert { background: #eff6ff; border-left: 4px solid #4f46e5; padding: 15px; margin: 20px 0; }
        .chart-container { text-align: center; margin: 20px 0; }
        .chart-container img { max-width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--border); }
    </style>
</head>
<body>
    <div class="container">
        <h1>每日全球策略量化报告</h1>
"""

    html += (
        "<div id='report-metadata' "
        f"data-run-id='{html_lib.escape(run_id or '', quote=True)}' "
        f"data-signal-date='{html_lib.escape(str(snapshot_date), quote=True)}' "
        f"data-generated-at='{html_lib.escape(generated_at, quote=True)}'>"
        f"<p>运行 ID：<code>{html_lib.escape(run_id or 'unbound')}</code><br>"
        f"信号日期：<code>{html_lib.escape(str(snapshot_date))}</code><br>"
        f"报告生成时间（UTC）：<code>{html_lib.escape(generated_at)}</code></p>"
        "</div>\n"
    )
    html += render_cash_overview_html(accounts, nav_status)
    html += render_settlement_status_html(settlement_status)

    html += "<h2>🟢 第一章：稳健红利策略 (基本面护城河)</h2>\n"
    html += subsection_html("dividend_a_stock", div_headers)

    html += "<hr>\n<h2>🔵 第二章：高增成长策略 (基本面护城河)</h2>\n"
    html += subsection_html("growth_a_stock", gro_headers)
    html += subsection_html("growth_us_stock", gro_headers)
    html += subsection_html("growth_hk_stock", gro_headers)

    html += "<h2>🔴 第三章：产业热点战法 (AI 宏观洞察与事件驱动)</h2>\n"
    html += subsection_html("hot_spot_a_stock", hot_headers)
    html += subsection_html("hot_spot_us_stock", hot_headers)
    html += subsection_html("hot_spot_hk_stock", hot_headers)

    html += "<h2>🌟 四、全策略综合对比总结 (Master Chart)</h2>\n"
    if os.path.exists(os.path.join(base_dir, "nav_chart_all.png")):
        html += get_chart_html("nav_chart_all.png", base_dir)
    html += get_chart_html("pnl_chart_all.png", base_dir)

    html += "    </div>\n</body>\n</html>\n"
    with open(output_html_file, "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
