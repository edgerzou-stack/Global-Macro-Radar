import sys
import os
import base64
import datetime as dt
import html as html_lib
from dataclasses import dataclass

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from llm_utils import call_llm, configured_quant_llm_identities
except ImportError:
    call_llm = None
    configured_quant_llm_identities = lambda: []
from report_review_service import DEFAULT_PROMPT_VERSION, ReportReviewService
from report_repository import (
    NAV_RUN_STATUS_PREFIX,
    SETTLEMENT_RUN_STATUS_PREFIX,
    ReportRepositoryError,
    ReportRepository,
)
try:
    from get_stock_name import get_stock_name, prime_stock_names
except ImportError:
    def get_stock_name(code, *, allow_network=True):
        return str(code)

    def prime_stock_names(names, *, persist=False):
        return None

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
REPORT_REVIEW_PROMPT_VERSION = DEFAULT_PROMPT_VERSION
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
NAV_STATUS_LABELS = {
    "fresh": "本次运行已重估",
    "certified_carry_forward": "沿用最近认证快照",
    "unavailable": "不可估值",
    "status_record_missing": "状态记录缺失",
}

DIVIDEND_HEADERS = ["股票代码", "股票简称", "PE", "PB", "估值公式值", "TTM股息率", "3年净利润CAGR", "入选日期", "入选价格", "仓位份数", "累计涨跌幅"]
GROWTH_HEADERS = ["股票代码", "股票简称", "PE", "净资产收益率", "营业总收入同比增长率", "净利润同比增长率", "入选日期", "入选价格", "仓位份数", "累计涨跌幅"]
HOT_SPOT_HEADERS = ["股票代码", "股票简称", "最新价", "涨跌幅(%)", "成交额(亿)", "入选日期", "入选价格", "仓位份数", "累计涨跌幅", "入选理由"]

REPORT_SECTIONS = (
    ("🟢 第一章：稳健红利策略 (基本面护城河)", (("dividend_a_stock", DIVIDEND_HEADERS),)),
    (
        "🔵 第二章：高增成长策略 (基本面护城河)",
        (
            ("growth_a_stock", GROWTH_HEADERS),
            ("growth_us_stock", GROWTH_HEADERS),
            ("growth_hk_stock", GROWTH_HEADERS),
        ),
    ),
    (
        "🔴 第三章：产业热点战法 (AI 宏观洞察与事件驱动)",
        (
            ("hot_spot_a_stock", HOT_SPOT_HEADERS),
            ("hot_spot_us_stock", HOT_SPOT_HEADERS),
            ("hot_spot_hk_stock", HOT_SPOT_HEADERS),
        ),
    ),
)


@dataclass(frozen=True)
class ReportViewModel:
    """Run-bound inputs shared by every report presentation."""

    run_id: str
    generated_at: str
    snapshot_date: str
    base_dir: str
    accounts: dict
    nav_status: dict
    settlement_status: dict
    results: dict
    diff: dict
    appendix: dict
    portfolio: dict
    trade_history: tuple
    execution_summary: dict
    pending_intents: object
    filled_intents: object
    unified_trades: object
    llm_reviews: dict
    code_map: dict


ReportDataError = ReportRepositoryError


UNRECORDED_ELIMINATION_REASON = "本次筛选未保留；运行结果未记录具体原因"
UNKNOWN_STOCK_NAME = "名称待同步"


def recorded_elimination_reason(item):
    """Use run-bound evidence; report rendering must not re-query live markets."""
    if not isinstance(item, dict):
        return UNRECORDED_ELIMINATION_REASON
    for field in ("reason", "剔除原因", "说明"):
        value = str(item.get(field) or "").strip()
        if value and value.lower() != "none":
            return value
    return UNRECORDED_ELIMINATION_REASON


def display_stock_name(code, code_map=None):
    """Resolve a presentation-only name without adding network dependencies."""
    symbol = str(code or "").strip()
    if not symbol:
        return ""
    mapped = (code_map or {}).get(symbol)
    if mapped and str(mapped).strip() != symbol:
        return str(mapped).strip()
    cached = str(
        get_stock_name(symbol, allow_network=False) or ""
    ).strip()
    return cached if cached and cached != symbol else UNKNOWN_STOCK_NAME


def strategy_display(strategy_id):
    strategy_id = str(strategy_id)
    return f"{STRAT_NAMES.get(strategy_id, strategy_id)} ({strategy_id})"



def load_active_strategy_accounts(db_path=None):
    return ReportRepository(db_path).load_active_strategy_accounts()


def load_run_status(prefix, run_id, db_path=None):
    return ReportRepository(db_path).load_run_status(prefix, run_id)


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
    return ReportRepository(db_path).load_execution_ledger_summary()


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
        symbol = str(code)
        candidate = candidate_by_code.get(symbol, {})
        candidate_name = str(candidate.get("股票简称") or "").strip()
        if candidate_name and candidate_name != symbol:
            name = candidate_name
        else:
            name = display_stock_name(symbol, code_map)
        rows.append(
            {
                "股票代码": symbol,
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


def _pending_intent_note(row, settlement_status=None):
    intent_id = str(row["intent_id"])
    for market in (settlement_status or {}).get("markets", {}).values():
        events = market.get("events") or {}
        for deferred in events.get("deferred") or []:
            if str(deferred.get("intent_id")) == intent_id:
                return "原始开盘价证据暂不可用，交割延期"
        for blocked in events.get("blocked") or []:
            if str(blocked.get("intent_id")) == intent_id:
                return "交割受阻，未修改持仓或现金"
        for rescheduled in events.get("rescheduled") or []:
            if str(rescheduled.get("intent_id")) == intent_id:
                return (
                    "受交易规则约束，最早交割日顺延至"
                    f"{rescheduled.get('rescheduled_session', '')}"
                )
    reason = str(row["reason"] or "")
    if reason.startswith("fixed-tranche drawdown rule met"):
        return "固定份额回撤加仓条件已满足，等待最早交割日的未复权开盘价"
    if reason.startswith("quantitative target change"):
        return "目标变化已生成不可变交割承诺，等待最早交割日的未复权开盘价"
    return reason or "等待最早交割日的未复权开盘价"


def format_pending_trade_intents(
    evidence,
    code_map=None,
    settlement_status=None,
):
    if evidence.pending is None:
        return None
    result = {strategy: [] for strategy in STRAT_NAMES}
    for row in evidence.pending:
        symbol = str(row["symbol"])
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
                "股票名称": display_stock_name(symbol, code_map),
                "信号日": str(row["signal_date"]),
                "状态": "待交割",
                "最早交割日": row["eligible_session"],
                "说明": (
                    f"{_pending_intent_note(row, settlement_status)}；"
                    f"来源运行={row['source_run_id']}"
                ).strip("；"),
            }
        )
    return result


def format_filled_trade_intents(evidence, code_map=None):
    if evidence.filled is None:
        return None
    result = {strategy: [] for strategy in STRAT_NAMES}
    for row in evidence.filled:
        symbol = str(row["symbol"])
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
                "股票名称": display_stock_name(symbol, code_map),
                "状态": "已按开盘价结算",
                "成交日": str(row["execution_session"]),
                "成交价格": f"{float(row['execution_price']):.2f}",
                "投入份数": str(int(row["tranche_quantity"])),
                "价格字段": str(row["price_field"]),
                "复权口径": str(row["adjustment"]),
                "行情来源": str(row["provider"]),
                "证据SHA-256": str(row["payload_sha256"]),
            }
        )
    return result


def format_unified_trade_records(evidence, code_map=None):
    if not evidence.available:
        return None
    result = {strategy: [] for strategy in STRAT_NAMES}
    for row in evidence.filled or ():
        strategy = str(row["strategy_id"])
        if strategy not in result:
            continue
        symbol = str(row["symbol"])
        linked_pnl = row.get("linked_pnl")
        pnl_text = (
            f"{float(linked_pnl) * 100:+.2f}%"
            if linked_pnl is not None
            else "-"
        )
        result[strategy].append(
            {
                "发起策略": strategy_display(strategy),
                "记录ID": str(row["intent_id"]),
                "市场": str(row["market"]),
                "操作": {
                    "SELL_ALL": "卖出",
                    "BUY_NEW": "买入",
                    "ADD_TRANCHE": "加仓",
                }.get(row["action"], row["action"]),
                "股票代码": symbol,
                "股票名称": display_stock_name(symbol, code_map),
                "成交日": str(row["execution_session"]),
                "成交价格": f"{float(row['execution_price']):.2f}",
                "离场盈亏率": pnl_text,
                "投入份数": str(int(row["tranche_quantity"])),
                "行情来源": str(row["provider"]),
                "证据SHA-256": str(row["payload_sha256"]),
            }
        )

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
    for row in evidence.legacy:
        strategy = str(row["strategy"])
        if strategy not in result:
            continue
        symbol = str(row["name"])
        pnl = float(row["pnl"]) * 100 if row["pnl"] is not None else 0.0
        result[strategy].append(
            {
                "发起策略": strategy_display(strategy),
                "记录ID": f"legacy-trade:{int(row['id'])}",
                "市场": market_by_strategy[strategy],
                "操作": "卖出平仓",
                "股票代码": symbol,
                "股票名称": display_stock_name(symbol, code_map),
                "成交日": str(row["exit_date"]),
                "成交价格": (
                    f"{float(row['exit_price']):.2f}"
                    if row["exit_price"] is not None
                    and float(row["exit_price"]) > 0
                    else "N/A"
                ),
                "离场盈亏率": f"{pnl:+.2f}%",
                "投入份数": str(int(row["shares"] or 1)),
                "行情来源": "Legacy",
                "证据SHA-256": "无（历史账本未保存原始行情摘要）",
            }
        )
    for strategy in result:
        result[strategy].sort(
            key=lambda row: (row["成交日"], row["记录ID"]),
            reverse=True,
        )
    return result


def load_pending_trade_intents(db_path=None, code_map=None):
    evidence = ReportRepository(db_path).load_trade_evidence()
    return format_pending_trade_intents(evidence, code_map)


def load_filled_trade_intents(db_path=None, code_map=None):
    evidence = ReportRepository(db_path).load_trade_evidence()
    return format_filled_trade_intents(evidence, code_map)


def load_unified_trade_records(db_path=None, code_map=None):
    evidence = ReportRepository(db_path).load_trade_evidence()
    return format_unified_trade_records(evidence, code_map)


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
        name = display_stock_name(code, code_map)

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
        name = display_stock_name(code, code_map)

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

def get_chart_md(chart_name, base_dir, alt_text=None):
    chart_path = os.path.join(base_dir, "reports", chart_name)
    if not os.path.exists(chart_path):
        chart_path = os.path.join(base_dir, chart_name)
    if os.path.exists(chart_path):
        return f"![{alt_text or chart_name}]({chart_path})\n\n"
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


def render_master_charts_md(base_dir):
    """Render the certified run-level NAV chart as an early decision block."""
    charts = get_chart_md("nav_chart_all.png", base_dir, "账户净值曲线")
    if not charts:
        return ""
    return "## 📈 账户净值曲线\n\n" + charts


def render_master_charts_html(base_dir):
    """HTML counterpart of the certified early NAV-chart block."""
    charts = get_chart_html("nav_chart_all.png", base_dir)
    if not charts:
        return ""
    return "<h2>📈 账户净值曲线</h2>\n" + charts


def _pending_adjustment_groups(
    strategy_id,
    strat_diff,
    current_run_id,
    code_map,
):
    groups = {
        "buy": [],
        "legacy_buy": [],
        "add": [],
        "legacy_add": [],
        "sell": [],
        "legacy_sell": [],
    }
    strategy_reason = STRAT_REASONS.get(strategy_id, "策略量化指标")
    for bucket in ("added", "removed"):
        for item in strat_diff.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            source_run_id = str(item.get("source_run_id") or "")
            if (
                current_run_id
                and source_run_id
                and source_run_id != str(current_run_id)
            ):
                continue
            if str(item.get("state") or "PENDING") != "PENDING":
                continue
            action = str(item.get("action") or "")
            group = {
                "BUY_NEW": "buy",
                "ADD_TRANCHE": "add",
                "SELL_ALL": "sell",
            }.get(action)
            if group is None:
                legacy_reason = str(item.get("reason") or "")
                if "网格加仓" in legacy_reason:
                    group = "legacy_add"
                else:
                    group = (
                        "legacy_sell"
                        if bucket == "removed"
                        else "legacy_buy"
                    )
            code = str(item.get("name") or "")
            name = display_stock_name(code, code_map)
            display_name = f"{code} ({name})" if name != code else code
            eligible = str(item.get("eligible_session") or "待账本确认")
            if group in {"buy", "legacy_buy"}:
                reason = f"满足【{strategy_reason}】入选标准"
            elif group in {"add", "legacy_add"}:
                legacy_reason = str(item.get("reason") or "")
                reason = (
                    legacy_reason
                    if "网格加仓" in legacy_reason
                    else "达到固定份额回撤加仓阈值"
                )
            else:
                reason = "本次目标列表未包含该标的"
            groups[group].append(
                f"{display_name}（最早交割日：{eligible}；{reason}）"
            )
    return groups


def _render_batch_llm_reviews(response):
    strategy_reviews = (
        response.get("strategy_reviews") if isinstance(response, dict) else None
    )
    if not isinstance(strategy_reviews, dict):
        return {}
    html_outputs = {}
    for strat, strat_data in strategy_reviews.items():
        if not isinstance(strat_data, dict):
            continue
        raw_reviews = strat_data.get("reviews", [])
        if not isinstance(raw_reviews, list):
            continue
        reviews = []
        for raw_review in raw_reviews:
            if not isinstance(raw_review, dict):
                continue
            review = dict(raw_review)
            try:
                review["合计分"] = float(review.get("护城河打分", 0)) + float(
                    review.get("成长性打分", 0)
                )
            except (TypeError, ValueError):
                continue
            reviews.append(review)
        reviews.sort(key=lambda item: item.get("合计分", 0), reverse=True)

        html = "<div class='llm-review'>\n<h3>🤖 AI 质性点评与打分</h3>\n"
        html += (
            "<table>\n  <thead>\n    <tr>\n"
            "      <th>股票代码</th><th>股票简称</th>"
            "<th>护城河打分(1-5)</th><th>成长性打分(1-5)</th>"
            "<th>合计分(满分10)</th><th>一句话点评</th>\n"
            "    </tr>\n  </thead>\n  <tbody>\n"
        )
        for review in reviews:
            html += (
                "    <tr>\n"
                f"      <td>{html_lib.escape(str(review.get('股票代码', '')))}</td>"
                f"<td>{html_lib.escape(str(review.get('股票简称', '')))}</td>"
                f"<td>{html_lib.escape(str(review.get('护城河打分', '')))}</td>"
                f"<td>{html_lib.escape(str(review.get('成长性打分', '')))}</td>"
                f"<td>{review.get('合计分', 0):.1f}</td>"
                f"<td>{html_lib.escape(str(review.get('一句话点评', '')))}</td>\n"
                "    </tr>\n"
            )
        html += "  </tbody>\n</table>\n"
        summary = strat_data.get("summary")
        if summary:
            html += (
                "<p><strong>总评：</strong>"
                f"{html_lib.escape(str(summary))}</p>\n"
            )
        html += "</div>\n"
        html_outputs[strat] = html
    return html_outputs


def generate_batch_llm_reviews(strategies_dict):
    if not strategies_dict or not call_llm:
        return {}
    response = ReportReviewService(
        call_llm=call_llm,
        configured_identities=configured_quant_llm_identities,
        prompt_version=REPORT_REVIEW_PROMPT_VERSION,
    ).get_reviews(strategies_dict)
    return _render_batch_llm_reviews(response)

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
    current_run_id=None,
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

    adjustment_groups = _pending_adjustment_groups(
        strategy_id,
        diff.get(strategy_id, {}),
        current_run_id,
        code_map,
    )
    if any(adjustment_groups.values()):
        out += "> **本次目标变化（尚未成交）**：\n"
        for key, icon, label in (
            ("buy", "🟢", "本次待买入"),
            ("legacy_buy", "🟢", "新增入池"),
            ("add", "🔵", "本次待加仓"),
            ("legacy_add", "🔵", "网格加仓"),
            ("sell", "🔴", "本次待卖出"),
            ("legacy_sell", "🔴", "掉出观测"),
        ):
            if adjustment_groups[key]:
                out += (
                    f"> {icon} **{label}**："
                    f"{', '.join(adjustment_groups[key])}\n"
                )
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
    current_run_id=None,
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

    adjustment_groups = _pending_adjustment_groups(
        strategy_id,
        diff.get(strategy_id, {}),
        current_run_id,
        code_map,
    )
    if any(adjustment_groups.values()):
        html += (
            "<div class='alert'>\n"
            "  <p><strong>本次目标变化（尚未成交）：</strong></p>\n"
        )
        for key, icon, label in (
            ("buy", "🟢", "本次待买入"),
            ("legacy_buy", "🟢", "新增入池"),
            ("add", "🔵", "本次待加仓"),
            ("legacy_add", "🔵", "网格加仓"),
            ("sell", "🔴", "本次待卖出"),
            ("legacy_sell", "🔴", "掉出观测"),
        ):
            if adjustment_groups[key]:
                html += (
                    f"  <p>{icon} <strong>{label}</strong>："
                    f"{', '.join(adjustment_groups[key])}</p>\n"
                )
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


def _render_subsection(view, strategy_id, headers, *, html=False):
    renderer = generate_subsection_html if html else generate_subsection_md
    return renderer(
        strategy_id,
        view.results.get(strategy_id, []),
        headers,
        view.diff,
        view.trade_history,
        view.base_dir,
        view.llm_reviews.get(strategy_id, ""),
        view.code_map,
        view.appendix.get(strategy_id, []),
        positions=view.portfolio.get(strategy_id, {}),
        execution_summary=view.execution_summary,
        pending_intents=(
            None
            if view.pending_intents is None
            else view.pending_intents.get(strategy_id, [])
        ),
        filled_intents=(
            None
            if view.filled_intents is None
            else view.filled_intents.get(strategy_id, [])
        ),
        unified_trades=(
            None
            if view.unified_trades is None
            else view.unified_trades.get(strategy_id, [])
        ),
        current_run_id=view.run_id,
    )


def render_report_markdown(view):
    out = "# 每日全球策略量化报告\n\n"
    out += (
        f"> 运行 ID：`{_safe_md(view.run_id or 'unbound')}`  \n"
        f"> 信号日期：`{_safe_md(view.snapshot_date)}`  \n"
        f"> 报告生成时间（UTC）：`{_safe_md(view.generated_at)}`\n\n"
    )
    out += render_cash_overview_md(view.accounts, view.nav_status)
    out += render_settlement_status_md(view.settlement_status)
    out += render_master_charts_md(view.base_dir)

    for index, (title, strategies) in enumerate(REPORT_SECTIONS):
        if index:
            out += "---\n\n"
        out += f"## {title}\n\n"
        for strategy_id, headers in strategies:
            out += _render_subsection(view, strategy_id, headers)
    return out


def render_report_html(view):
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
        f"data-run-id='{html_lib.escape(view.run_id or '', quote=True)}' "
        f"data-signal-date='{html_lib.escape(str(view.snapshot_date), quote=True)}' "
        f"data-generated-at='{html_lib.escape(view.generated_at, quote=True)}'>"
        f"<p>运行 ID：<code>{html_lib.escape(view.run_id or 'unbound')}</code><br>"
        f"信号日期：<code>{html_lib.escape(str(view.snapshot_date))}</code><br>"
        f"报告生成时间（UTC）：<code>{html_lib.escape(view.generated_at)}</code></p>"
        "</div>\n"
    )
    html += render_cash_overview_html(view.accounts, view.nav_status)
    html += render_settlement_status_html(view.settlement_status)
    html += render_master_charts_html(view.base_dir)

    for title, strategies in REPORT_SECTIONS:
        html += f"<h2>{title}</h2>\n"
        for strategy_id, headers in strategies:
            html += _render_subsection(
                view,
                strategy_id,
                headers,
                html=True,
            )
    html += "    </div>\n</body>\n</html>\n"
    return html


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 generate_report.py <input_json> <output_md>")
        sys.exit(1)

    input_file = sys.argv[1]
    output_md_file = sys.argv[2]
    base_dir = os.path.dirname(input_file)
    output_html_file = os.path.splitext(output_md_file)[0] + ".html"

    run_id = _report_run_id()
    generated_at = _report_generated_at()
    run_snapshot = ReportRepository().load_snapshot(
        run_id=run_id,
        generated_at=generated_at,
    )
    payload = run_snapshot.mutable_daily_payload()
    if not payload:
        print("No daily_results found in strategy_daily_results table")
        sys.exit(1)

    portfolio = run_snapshot.portfolio
    trade_history = run_snapshot.trade_history
    results = payload.get("results", {})
    diff = payload.get("diff", {})
    appendix = payload.get("appendix", {})
    nav_status = run_snapshot.nav_status
    settlement_status = run_snapshot.settlement_status
    execution_summary = run_snapshot.execution_summary
    code_map = {}
    for strat, items in results.items():
        for item in items:
            code = item.get("股票代码")
            name = item.get("股票简称")
            symbol = str(code or "").strip()
            display_name = str(name or "").strip()
            if symbol and display_name and display_name != symbol:
                code_map[symbol] = display_name

    prime_stock_names(
        code_map,
        persist=os.environ.get("QUANT_DB_ENV") == "production",
    )

    pending_intents = format_pending_trade_intents(
        run_snapshot.trade_evidence,
        code_map,
        settlement_status,
    )
    filled_intents = format_filled_trade_intents(
        run_snapshot.trade_evidence,
        code_map,
    )
    unified_trades = format_unified_trade_records(
        run_snapshot.trade_evidence,
        code_map,
    )

    snapshot_date = run_snapshot.snapshot_date or "1970-01-01"

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

    view = ReportViewModel(
        run_id=run_id or "",
        generated_at=generated_at,
        snapshot_date=snapshot_date,
        base_dir=base_dir,
        accounts=run_snapshot.accounts,
        nav_status=nav_status,
        settlement_status=settlement_status,
        results=results,
        diff=diff,
        appendix=appendix,
        portfolio=portfolio,
        trade_history=tuple(trade_history),
        execution_summary=execution_summary,
        pending_intents=pending_intents,
        filled_intents=filled_intents,
        unified_trades=unified_trades,
        llm_reviews=llm_reviews,
        code_map=code_map,
    )
    out = render_report_markdown(view)

    os.makedirs(os.path.dirname(output_md_file), exist_ok=True)
    with open(output_md_file, "w", encoding="utf-8") as f:
        f.write(out)

    html = render_report_html(view)
    with open(output_html_file, "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    main()
