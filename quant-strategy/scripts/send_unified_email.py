"""Build and deliver the unified report through an explicit delivery mode.

Non-production runs use the local sink. Live delivery is journaled by run ID.
``accepted_by_smtp`` means only that the outbound server reported no immediate
recipient refusal; it does not claim inbox delivery. An interrupted ``sending``
state is deliberately ambiguous and therefore requires operator reconciliation
rather than risking a duplicate message.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import json
import os
import re
import smtplib
import ssl
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email import policy
from email.utils import format_datetime
from pathlib import Path

import markdown
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError


VALID_DELIVERY_MODES = {"disabled", "sink", "live"}
DELIVERY_JOURNAL_SCHEMA_VERSION = 2
EMAIL_MIME_SCHEMA_VERSION = 3
EMAIL_DELIVERY_PROFILE = "icloud_safe_v1"
DEFAULT_LIVE_SUBJECT = "Global Macro Radar Daily Report"
MAX_INLINE_IMAGE_WIDTH = 1600
MAX_INLINE_IMAGE_BYTES = 128 * 1024
MAX_LIVE_MESSAGE_BYTES = 512 * 1024
INLINE_IMAGE_PALETTE_COLORS = 256
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
DATA_IMAGE_PATTERN = re.compile(
    r"data:image/(?P<subtype>[A-Za-z0-9.+-]+);base64,"
    r"(?P<payload>[A-Za-z0-9+/=\s]+)"
)
RECIPIENT_HISTORY_LIMIT = 10
RADAR_SECTION_MARKER = "全球前沿产业雷达"
MIN_RADAR_ARTICLES = 1
RECIPIENT_DEBUG_TERMS = (
    "运行 ID",
    "报告生成时间（UTC）",
    "Sandbox Benchmark Engine",
    "legacy portfolio",
    "v7 trade_intents",
    "v8 evidence",
    "证据SHA-256",
    "行情来源",
    "来源运行=",
    "prompt_version",
    "llm_provider",
    "llm_degraded",
)
RECIPIENT_STRATEGY_NAMES = {
    "dividend_a_stock": "A股核心红利精选",
    "growth_a_stock": "A股高增成长精选",
    "growth_us_stock": "美股高增成长精选",
    "growth_hk_stock": "港股高增成长精选",
    "hot_spot_a_stock": "A股热点突击",
    "hot_spot_us_stock": "美股热点突击",
    "hot_spot_hk_stock": "港股热点突击",
}


class DeliveryError(RuntimeError):
    pass


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_journal(path: Path, payload: dict) -> None:
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
    )


@contextmanager
def _delivery_lock(path: Path):
    """Serialize journal checks and side effects for one run across processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _delivery_locks(run_lock: Path, scope_lock: Path | None = None):
    """Lock the recipient/date scope before the individual run journal."""
    if scope_lock is None:
        with _delivery_lock(run_lock):
            yield
        return
    with _delivery_lock(scope_lock):
        with _delivery_lock(run_lock):
            yield


def _normalise_effective_date(value):
    if value in {None, ""}:
        return None
    text = str(value)
    try:
        date.fromisoformat(text)
    except ValueError as error:
        raise DeliveryError(
            f"Invalid report effective date {text!r}; expected YYYY-MM-DD"
        ) from error
    return text


def _dated_subject(subject, effective_date):
    if not effective_date:
        return subject
    suffix = f" | {effective_date}"
    return subject if str(subject).endswith(suffix) else f"{subject}{suffix}"


def _daily_delivery_scope_lock(artifact_dir, *, recipient, effective_date):
    identity = json.dumps(
        {
            "recipient": recipient,
            "effective_date": effective_date,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    artifact_root = Path(artifact_dir).expanduser().resolve().parent
    return artifact_root / ".delivery-dedupe" / f"{digest}.lock"


def _find_prior_daily_live_delivery(
    artifact_dir,
    *,
    current_journal,
    recipient,
    effective_date,
    allow_terminal_duplicate=False,
):
    artifact_root = Path(artifact_dir).expanduser().resolve().parent
    current_journal = current_journal.resolve()
    for path in sorted(artifact_root.glob("*/delivery/*.json")):
        if path.resolve() == current_journal:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DeliveryError(
                f"Unreadable delivery journal during daily dedupe: {path}"
            ) from error
        if (
            payload.get("mode") != "live"
            or payload.get("recipient") != recipient
            or payload.get("effective_date") != effective_date
        ):
            continue
        state = payload.get("state")
        if state in {
            "accepted_by_smtp",
            "confirmed_received",
            "delivered",
        }:
            if allow_terminal_duplicate:
                continue
            raise DeliveryError(
                f"{recipient} already has a terminal live delivery for "
                f"{effective_date}: run_id={payload.get('run_id')!r} "
                f"state={state!r}"
            )
        if state == "sending":
            raise DeliveryError(
                f"{recipient} has an ambiguous live delivery for "
                f"{effective_date}: run_id={payload.get('run_id')!r}; "
                "reconcile it before any retry"
            )


def check_daily_delivery_gate(
    *,
    artifact_dir,
    recipient,
    effective_date,
    allow_duplicate_effective_date=False,
):
    """Fail before production work when the daily live-delivery gate is closed."""
    effective_date = _normalise_effective_date(effective_date)
    recipient = str(recipient or "").strip()
    if not artifact_dir:
        raise DeliveryError("Daily delivery preflight requires an artifact directory")
    if not recipient:
        raise DeliveryError("Daily delivery preflight requires a recipient")
    if not effective_date:
        raise DeliveryError("Daily delivery preflight requires an effective date")

    preflight_dir = Path(artifact_dir).expanduser().resolve()
    current_journal = preflight_dir / "delivery" / ".preflight.json"
    scope_lock = _daily_delivery_scope_lock(
        preflight_dir,
        recipient=recipient,
        effective_date=effective_date,
    )
    with _delivery_lock(scope_lock):
        _find_prior_daily_live_delivery(
            preflight_dir,
            current_journal=current_journal,
            recipient=recipient,
            effective_date=effective_date,
            allow_terminal_duplicate=allow_duplicate_effective_date,
        )
    return {
        "state": "delivery_gate_clear",
        "recipient": recipient,
        "effective_date": effective_date,
        "duplicate_effective_date_override": bool(
            allow_duplicate_effective_date
        ),
    }


def get_latest_radar_report(reports_dir=None, effective_date=None):
    default_reports_dir = Path(__file__).resolve().parents[2] / "industry-radar" / "reports"
    directory = Path(
        reports_dir or os.environ.get("RADAR_REPORTS_DIR", default_reports_dir)
    )
    date_text = effective_date or os.environ.get("PIPELINE_EFFECTIVE_DATE") or os.environ.get(
        "EFFECTIVE_DATE"
    )
    if date_text:
        try:
            date.fromisoformat(date_text)
        except ValueError as error:
            raise DeliveryError(
                f"Invalid report effective date {date_text!r}; expected YYYY-MM-DD"
            ) from error
        report = directory / f"industry_report_{date_text}.md"
        if not report.is_file():
            raise DeliveryError(
                f"Radar report for effective date {date_text} does not exist: {report}"
            )
        return str(report)
    reports = sorted(directory.glob("*.md"), reverse=True)
    return str(reports[0]) if reports else None


def _load_delivery_config(config_path=None):
    default_path = Path(__file__).resolve().parents[2] / "industry-radar" / "config.yaml"
    path = Path(config_path or os.environ.get("RADAR_CONFIG", default_path))
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("delivery", {})


def _load_runtime_environment(env_path=None):
    explicit = env_path or os.environ.get("RADAR_ENV")
    path = Path(explicit) if explicit else Path(__file__).resolve().parents[2] / ".env"
    if path.is_file():
        load_dotenv(path, override=False)
        return path
    if explicit:
        raise DeliveryError(f"Configured RADAR_ENV does not exist: {path}")
    return None


def _strip_existing_radar_sections(html_content):
    """Make radar injection idempotent when a prior unified file is reused."""
    soup = BeautifulSoup(html_content, "html.parser")
    for heading in list(
        soup.find_all(
            "h2", string=lambda value: value and "全球前沿产业雷达" in value
        )
    ):
        radar = heading.find_next_sibling("div")
        divider = radar.find_next_sibling("hr") if radar is not None else None
        heading.extract()
        if radar is not None:
            radar.extract()
        if divider is not None:
            divider.extract()
    return str(soup)


def _insert_radar_before_strategy_sections(html_content, radar_html):
    """Keep account/curve overview first and place news before strategy detail."""
    soup = BeautifulSoup(html_content, "html.parser")
    fragment = BeautifulSoup(radar_html, "html.parser")
    first_strategy = soup.find(
        "h2",
        string=lambda value: value
        and any(
            marker in value
            for marker in ("稳健红利策略", "高增成长策略", "产业热点战法")
        ),
    )
    if first_strategy is None:
        container = soup.select_one(".container") or soup.body or soup
        for node in list(fragment.contents):
            container.append(node.extract())
        return str(soup)

    for node in list(fragment.contents):
        first_strategy.insert_before(node.extract())
    return str(soup)


def load_approved_html(path, expected_sha256=None):
    html_path = Path(path).expanduser().resolve()
    if not html_path.is_file():
        raise DeliveryError(f"Approved HTML file does not exist: {html_path}")
    try:
        html_content = html_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise DeliveryError(f"Unable to read approved HTML file: {html_path}") from error
    digest = hashlib.sha256(html_content.encode("utf-8")).hexdigest()
    if expected_sha256 is not None:
        if not SHA256_PATTERN.fullmatch(expected_sha256):
            raise DeliveryError("Expected HTML SHA-256 must contain exactly 64 hex digits")
        if digest != expected_sha256.lower():
            raise DeliveryError(
                f"Approved HTML SHA-256 mismatch: expected={expected_sha256.lower()} "
                f"actual={digest}"
            )
    return html_content, digest


def build_unified_html(project_root=None, radar_report=None, effective_date=None):
    root = Path(project_root or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[1])
    quant_html = root / "quant-strategy" / "reports" / "screening_results.html"
    if not quant_html.exists():
        quant_html = root / "reports" / "screening_results.html"
    radar_report = radar_report or get_latest_radar_report(
        effective_date=effective_date
    )

    radar_html = ""
    if radar_report and Path(radar_report).exists():
        radar_md = Path(radar_report).read_text(encoding="utf-8")
        radar_md = re.sub(r"!\[.*?\]\(.*?\)", "", radar_md)
        rendered = markdown.markdown(radar_md, extensions=["tables", "md_in_html"])
        radar_html = (
            "<h2>🌍 第一部分：全球前沿产业雷达</h2>\n"
            f"<div style='margin-bottom: 40px;'>{rendered}</div>\n<hr>\n"
        )

    if quant_html.exists():
        html_content = quant_html.read_text(encoding="utf-8")
        html_content = _strip_existing_radar_sections(html_content)
        if radar_html:
            html_content = _insert_radar_before_strategy_sections(
                html_content,
                radar_html,
            )
        return html_content
    return (
        "<html><body><div class='container'>"
        "<h1>每日全球策略量化报告</h1>"
        f"{radar_html}<p>No quant report found.</p>"
        "</div></body></html>"
    )


def _remove_sibling_section(heading):
    """Remove one heading and its direct siblings up to the next heading."""
    current = heading.next_sibling
    while current is not None:
        following = current.next_sibling
        if getattr(current, "name", None) in {"h1", "h2", "h3", "h4"}:
            break
        current.extract()
        current = following
    heading.extract()


def _keep_table_columns(table, keep_headers):
    headers = table.select("thead th")
    indexes = [
        index
        for index, header in enumerate(headers)
        if header.get_text(" ", strip=True) in keep_headers
    ]
    if not indexes:
        return
    for row in table.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        for index in reversed(range(len(cells))):
            if index not in indexes:
                cells[index].extract()


def _remove_radar_deep_dive(radar_container):
    deep_heading = radar_container.find(
        "h2", string=lambda value: value and "深度研报" in value
    )
    if deep_heading is not None:
        current = deep_heading.next_sibling
        while current is not None:
            following = current.next_sibling
            if (
                getattr(current, "name", None) == "h2"
                and any(
                    marker in current.get_text(" ", strip=True)
                    for marker in ("顶流硬核", "科技硬核创新", "产业焦点")
                )
            ):
                break
            current.extract()
            current = following
        deep_heading.extract()

    title = radar_container.find(
        "h1", string=lambda value: value and "科技产业情报雷达" in value
    )
    if title is not None:
        title.extract()
    for article_heading in radar_container.find_all("h3"):
        candidate = article_heading.find_next_sibling()
        if candidate is not None and candidate.name == "p" and candidate.find("em"):
            candidate.extract()


def build_recipient_html(full_html):
    """Create a decision-focused email while preserving the full audit report."""
    soup = BeautifulSoup(full_html, "html.parser")
    container = soup.select_one(".container") or soup.body
    if container is None:
        raise DeliveryError("Full report has no HTML body")

    main_title = container.find("h1", recursive=False) or container.find("h1")
    metadata = soup.find(id="report-metadata")
    report_date = ""
    if metadata is not None:
        report_date = str(metadata.get("data-signal-date") or "")
        metadata.clear()
        summary = soup.new_tag("p")
        summary["class"] = "recipient-report-date"
        summary.string = f"报告日期：{report_date or '未提供'}"
        metadata.append(summary)
        metadata.attrs = {
            "id": "report-metadata",
            "data-signal-date": report_date,
        }
    if main_title is not None:
        intro = soup.new_tag("p")
        intro["class"] = "recipient-summary"
        intro.string = (
            "本邮件依次展示账户净值、本次交割、产业事件、"
            "当前持仓与策略候选；"
            "完整历史和执行证据保留在本地审计报告中。"
        )
        main_title.insert_after(intro)

    nav_heading = soup.find(
        "h2", string=lambda value: value and "台账概览" in value
    )
    if nav_heading is not None:
        nav_heading.string = "🏦 账户与净值概览"
        nav_table = nav_heading.find_next("table")
        if nav_table is not None:
            _keep_table_columns(
                nav_table,
                {
                    "策略沙盒 (Strategy)",
                    "当前可用现金",
                    "认证 NAV",
                    "账本资金占用率",
                    "NAV 状态",
                },
            )
            first_header = nav_table.find("th")
            if first_header is not None:
                first_header.string = "策略"
            for row in nav_table.select("tbody tr"):
                first_cell = row.find("td")
                if first_cell is None:
                    continue
                strategy_id = row.get("data-strategy") or first_cell.get_text(
                    " ", strip=True
                )
                first_cell.string = RECIPIENT_STRATEGY_NAMES.get(
                    strategy_id, strategy_id
                )
        alert = nav_heading.find_next(
            class_=lambda value: value and "alert" in str(value).split()
        )
        if (
            alert is not None
            and nav_table is not None
            and "沿用最近认证快照"
            not in nav_table.get_text(" ", strip=True)
        ):
            alert.extract()

    heading_replacements = {
        "实际持仓（legacy portfolio）": "当前持仓",
        "本次筛选候选（研究池，未必已成交）": "本次策略候选（未成交）",
        "待交割指令（v7 trade_intents）": "待交割指令",
        "交易执行与历史成交明细（v7 trade_intents + v8 evidence + legacy）": (
            "最近交割记录（最多10笔）"
        ),
    }
    for heading in soup.find_all("h4"):
        original = heading.get_text(" ", strip=True)
        replacement = heading_replacements.get(original)
        if replacement is not None:
            heading.string = replacement
        if replacement == "待交割指令":
            table = heading.find_next_sibling("table")
            if table is not None:
                for cell in table.find_all("td"):
                    cleaned = re.sub(
                        r"(?:[；;]\s*)?来源运行=[^；;]+", "", cell.get_text()
                    ).strip("；; ")
                    if cleaned != cell.get_text():
                        cell.string = cleaned or "-"
        if replacement == "最近交割记录（最多10笔）":
            table = heading.find_next_sibling("table")
            if table is not None:
                _keep_table_columns(
                    table,
                    {
                        "操作",
                        "股票代码",
                        "股票名称",
                        "成交日",
                        "成交价格",
                        "离场盈亏率",
                        "投入份数",
                    },
                )
                for row in table.select("tbody tr")[RECIPIENT_HISTORY_LIMIT:]:
                    row.extract()

    for chart_heading in list(
        soup.find_all("h4", string=lambda value: value and "资金净值曲线图" in value)
    ):
        _remove_sibling_section(chart_heading)

    # Defense in depth for a stale full-report artifact generated before the
    # invalid trade-percentage master chart was retired.
    for image in list(soup.find_all("img", alt="pnl_chart_all.png")):
        container = image.find_parent(
            "div",
            class_=lambda value: value
            and "chart-container" in str(value).split(),
        )
        (container or image).extract()
    retired_heading = soup.find(
        "h2",
        string=lambda value: value and "账户净值与回测曲线" in value,
    )
    if retired_heading is not None:
        retired_heading.string = "📈 账户净值曲线"

    radar_heading = soup.find(
        "h2", string=lambda value: value and "全球前沿产业雷达" in value
    )
    if radar_heading is not None:
        radar_container = radar_heading.find_next_sibling("div")
        if radar_container is not None:
            _remove_radar_deep_dive(radar_container)
        radar_nodes = [radar_heading]
        if radar_container is not None:
            radar_nodes.append(radar_container)
            divider = radar_container.find_next_sibling("hr")
            if divider is not None:
                radar_nodes.append(divider)
        first_strategy = soup.find(
            "h2",
            string=lambda value: value
            and any(
                marker in value
                for marker in ("稳健红利策略", "高增成长策略", "产业热点战法")
            ),
        )
        if first_strategy is not None:
            for node in radar_nodes:
                first_strategy.insert_before(node.extract())

    compact = str(soup)
    validate_recipient_html(compact)
    return compact


def _extract_radar_articles(soup):
    radar_heading = soup.find(
        "h2", string=lambda value: value and RADAR_SECTION_MARKER in value
    )
    if radar_heading is None:
        return []
    radar_container = radar_heading.find_next_sibling("div")
    if radar_container is None:
        return []

    articles = []
    for heading in radar_container.find_all("h3"):
        title = heading.get_text(" ", strip=True)
        if not title:
            continue
        article_url = ""
        detail_lines = []
        for sibling in heading.next_siblings:
            sibling_name = getattr(sibling, "name", None)
            if sibling_name in {"h2", "h3"}:
                break
            if sibling_name is not None:
                detail = sibling.get_text(" ", strip=True)
                if detail:
                    detail_lines.append(detail)
                link = sibling.find("a", href=True)
                if link is not None and not article_url:
                    article_url = str(link.get("href") or "").strip()
        articles.append(
            {
                "title": title,
                "url": article_url,
                "details": detail_lines,
            }
        )
    return articles


class RecipientReportAnalysis:
    __slots__ = (
        "soup",
        "visible_text",
        "articles",
        "report_date",
        "html_sha256",
    )

    def __init__(
        self, *, soup, visible_text, articles, report_date, html_sha256
    ):
        self.soup = soup
        self.visible_text = visible_text
        self.articles = articles
        self.report_date = report_date
        self.html_sha256 = html_sha256

    @property
    def radar_article_count(self):
        return len(self.articles)


def analyze_recipient_html(html_content):
    """Parse recipient HTML once and expose reusable delivery evidence."""
    soup = BeautifulSoup(html_content, "html.parser")
    metadata = soup.find(id="report-metadata")
    report_date = ""
    if metadata is not None:
        report_date = str(metadata.get("data-signal-date") or "").strip()
    return RecipientReportAnalysis(
        soup=soup,
        visible_text=soup.get_text(" ", strip=True),
        articles=tuple(_extract_radar_articles(soup)),
        report_date=report_date,
        html_sha256=hashlib.sha256(html_content.encode("utf-8")).hexdigest(),
    )


def build_plain_text_fallback(html_content, *, analysis=None):
    """Build a useful fallback for clients that decline the HTML alternative."""
    analysis = analysis or analyze_recipient_html(html_content)
    visible_text = analysis.soup.get_text("\n", strip=True)
    if not visible_text:
        return "Global Macro Radar report."

    articles = analysis.articles
    if not articles:
        return visible_text

    lines = ["Global Macro Radar 日报"]
    if analysis.report_date:
        lines.append(f"报告日期：{analysis.report_date}")
    lines.extend(["", "全球前沿产业雷达"])
    for article in articles:
        lines.append(f"- {article['title']}")
        if article["details"]:
            lines.append(f"  {article['details'][0]}")
        if article["url"]:
            lines.append(f"  {article['url']}")
    lines.extend(["", "完整账户、交割和策略表格请使用支持 HTML 的邮件客户端查看。"])
    return "\n".join(lines)


def validate_recipient_html(html_content, *, analysis=None):
    analysis = analysis or analyze_recipient_html(html_content)
    soup = analysis.soup
    visible_text = analysis.visible_text
    leaked = [term for term in RECIPIENT_DEBUG_TERMS if term in visible_text]
    if leaked:
        raise DeliveryError(
            "Recipient report contains debug-only material: " + ", ".join(leaked)
        )
    required = ("报告日期：", "账户与净值概览", "本次交割状态")
    missing = [term for term in required if term not in visible_text]
    if missing:
        raise DeliveryError(
            "Recipient report is missing decision content: " + ", ".join(missing)
        )
    if "深度研报" in visible_text:
        raise DeliveryError("Recipient report contains duplicated Deep Dive content")
    if soup.find("img", alt="pnl_chart_all.png") is not None:
        raise DeliveryError(
            "Recipient report contains the retired trade-percentage master chart"
        )
    if "账户净值与回测曲线" in visible_text:
        raise DeliveryError(
            "Recipient report contains the retired master-chart heading"
        )
    radar_heading = soup.find(
        "h2", string=lambda value: value and RADAR_SECTION_MARKER in value
    )
    if radar_heading is None:
        raise DeliveryError("Recipient report is missing the industry radar section")
    radar_container = radar_heading.find_next_sibling("div")
    if radar_container is None:
        raise DeliveryError("Recipient report has no industry radar content container")
    articles = analysis.articles
    if len(articles) < MIN_RADAR_ARTICLES:
        raise DeliveryError(
            "Recipient report contains no industry radar news articles"
        )
    if not any(article["url"] for article in articles):
        raise DeliveryError("Recipient report news articles contain no source links")
    for heading in soup.find_all(
        "h4", string="最近交割记录（最多10笔）"
    ):
        table = heading.find_next_sibling("table")
        if table is not None and len(table.select("tbody tr")) > RECIPIENT_HISTORY_LIMIT:
            raise DeliveryError("Recipient report contains excessive trade history")
    return True


def _load_existing_journal(path: Path, content_hash: str):
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeliveryError(f"Unreadable delivery journal: {path}") from error
    if payload.get("content_sha256") != content_hash:
        raise DeliveryError("Run ID was already used for different email content")
    state = payload.get("state")
    # ``delivered`` is the legacy name for an SMTP-accepted message.  Keep it
    # terminal for idempotency, but never emit it for a new send.
    if state in {"sink", "accepted_by_smtp", "confirmed_received", "delivered"}:
        return payload
    if state == "sending":
        raise DeliveryError(
            "Previous live delivery stopped in ambiguous 'sending' state; "
            "reconcile with SMTP provider before retrying"
        )
    return None


def reconcile_confirmed_delivery(
    *,
    artifact_dir,
    run_id: str,
    expected_html_sha256: str,
    expected_recipient: str,
    confirmed_by: str,
):
    """Record actual receipt only after the recipient confirms it."""
    if not run_id or not RUN_ID_PATTERN.fullmatch(run_id):
        raise DeliveryError(
            "RUN_ID must contain 1-128 safe alphanumeric/._:- characters"
        )
    if not SHA256_PATTERN.fullmatch(expected_html_sha256 or ""):
        raise DeliveryError("Expected HTML SHA-256 must contain exactly 64 hex digits")
    if not expected_recipient:
        raise DeliveryError("Expected recipient is required for delivery reconciliation")
    if confirmed_by != "recipient":
        raise DeliveryError("Delivery reconciliation requires recipient confirmation")

    output_dir = Path(artifact_dir).expanduser().resolve() / "delivery"
    journal_path = output_dir / f"{run_id}.json"
    lock_path = output_dir / f"{run_id}.lock"
    with _delivery_lock(lock_path):
        try:
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise DeliveryError(f"Delivery journal does not exist: {journal_path}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise DeliveryError(f"Unreadable delivery journal: {journal_path}") from error

        actual_hash = str(payload.get("html_sha256") or "").lower()
        if actual_hash != expected_html_sha256.lower():
            raise DeliveryError(
                "Delivery reconciliation HTML SHA-256 mismatch: "
                f"expected={expected_html_sha256.lower()} actual={actual_hash}"
            )
        actual_recipient = payload.get("recipient")
        if actual_recipient != expected_recipient:
            raise DeliveryError(
                "Delivery reconciliation recipient mismatch: "
                f"expected={expected_recipient!r} actual={actual_recipient!r}"
            )
        state = payload.get("state")
        if state in {"confirmed_received", "delivered"}:
            return {**payload, "duplicate": True}
        if state not in {"sending", "accepted_by_smtp"}:
            raise DeliveryError(
                "Only a 'sending' or 'accepted_by_smtp' journal can be "
                "reconciled as confirmed received; "
                f"actual state={state!r}"
            )

        reconciled = {
            **payload,
            "state": "confirmed_received",
            "safe_to_retry": False,
            "reconciled_from": state,
            "reconciliation": "recipient_confirmed_received",
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
            "confirmed_by": confirmed_by,
        }
        _write_journal(journal_path, reconciled)
        return reconciled


def _optimise_inline_image(subtype, image_bytes):
    """Return a bounded PNG suitable for a conservative mailbox MIME profile."""
    if subtype.lower() != "png":
        raise DeliveryError(
            f"Live delivery only supports PNG data images; found {subtype!r}"
        )
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            source.load()
            image = source.convert("RGBA")
    except (OSError, UnidentifiedImageError) as error:
        raise DeliveryError("Report contains an unreadable PNG data image") from error

    original_width, original_height = image.size
    if original_width <= 0 or original_height <= 0:
        raise DeliveryError("Report contains a PNG with invalid dimensions")
    if original_width > MAX_INLINE_IMAGE_WIDTH:
        target_height = max(
            1,
            round(original_height * MAX_INLINE_IMAGE_WIDTH / original_width),
        )
        image = image.resize(
            (MAX_INLINE_IMAGE_WIDTH, target_height),
            Image.Resampling.LANCZOS,
        )

    flattened = Image.new("RGB", image.size, "white")
    flattened.paste(image, mask=image.getchannel("A"))
    palette = flattened.quantize(
        colors=INLINE_IMAGE_PALETTE_COLORS,
        method=Image.Quantize.FASTOCTREE,
        dither=Image.Dither.NONE,
    )
    output = io.BytesIO()
    palette.save(output, format="PNG", optimize=True, compress_level=9)
    optimised = output.getvalue()
    if not optimised or len(optimised) > MAX_INLINE_IMAGE_BYTES:
        raise DeliveryError(
            "Optimised inline image exceeds the mailbox-safe size limit: "
            f"{len(optimised)} > {MAX_INLINE_IMAGE_BYTES} bytes"
        )
    return optimised, {
        "source_bytes": len(image_bytes),
        "delivered_bytes": len(optimised),
        "source_width": original_width,
        "source_height": original_height,
        "delivered_width": palette.width,
        "delivered_height": palette.height,
    }


def _build_live_message(
    *,
    html_content,
    subject,
    sender,
    recipient,
    run_id,
    analysis=None,
):
    inline_images = {}
    image_evidence = []

    def replace_data_image(match):
        subtype = match.group("subtype").lower()
        encoded = re.sub(r"\s+", "", match.group("payload"))
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise DeliveryError("Report contains an invalid base64 data image") from error
        if not image_bytes:
            raise DeliveryError("Report contains an empty base64 data image")
        optimised, evidence = _optimise_inline_image(subtype, image_bytes)
        digest = hashlib.sha256(optimised).hexdigest()
        cid = f"gmr-{digest[:24]}"
        if cid not in inline_images:
            inline_images[cid] = ("png", optimised)
            image_evidence.append({**evidence, "cid": cid})
        return f"cid:{cid}"

    rendered_html = DATA_IMAGE_PATTERN.sub(replace_data_image, html_content)
    analysis = analysis or analyze_recipient_html(html_content)
    plain_text = build_plain_text_fallback(html_content, analysis=analysis)
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    sender_domain = sender.rpartition("@")[2] or "localhost"
    message_identity = hashlib.sha256(
        json.dumps(
            {
                "run_id": run_id,
                "sender": sender,
                "recipient": recipient,
                "subject": subject,
                "html": html_content,
                "plain_text": plain_text,
                "mime_schema_version": EMAIL_MIME_SCHEMA_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    message["Message-ID"] = f"<gmr-{message_identity[:32]}@{sender_domain}>"
    message["X-Global-Macro-Radar-Run-ID"] = run_id
    message["X-Global-Macro-Radar-MIME-Schema"] = str(EMAIL_MIME_SCHEMA_VERSION)
    message["X-Global-Macro-Radar-Delivery-Profile"] = EMAIL_DELIVERY_PROFILE
    message["X-Global-Macro-Radar-Radar-Articles"] = str(
        analysis.radar_article_count
    )
    message.set_content(plain_text)
    message.add_alternative(rendered_html, subtype="html")
    html_part = message.get_payload()[-1]

    for index, (cid, (subtype, image_bytes)) in enumerate(
        inline_images.items(), start=1
    ):
        extension = "svg" if subtype == "svg+xml" else subtype
        html_part.add_related(
            image_bytes,
            maintype="image",
            subtype=subtype,
            cid=f"<{cid}>",
            disposition="inline",
            filename=f"chart-{index}.{extension}",
        )
    message_size_bytes = len(message.as_bytes(policy=policy.SMTP))
    if message_size_bytes > MAX_LIVE_MESSAGE_BYTES:
        raise DeliveryError(
            "Live email exceeds the mailbox-safe message size limit: "
            f"{message_size_bytes} > {MAX_LIVE_MESSAGE_BYTES} bytes"
        )
    return message, {
        "delivery_profile": EMAIL_DELIVERY_PROFILE,
        "inline_image_count": len(inline_images),
        "inline_image_source_bytes": sum(
            item["source_bytes"] for item in image_evidence
        ),
        "inline_image_delivered_bytes": sum(
            item["delivered_bytes"] for item in image_evidence
        ),
        "inline_images": image_evidence,
        "message_size_bytes": message_size_bytes,
        "message_size_limit_bytes": MAX_LIVE_MESSAGE_BYTES,
    }


def _serialise_refused_recipients(refused):
    """Convert smtplib's refusal mapping into stable JSON-safe evidence."""
    result = {}
    for address, detail in (refused or {}).items():
        try:
            code, response = detail
        except (TypeError, ValueError):
            code, response = None, detail
        if isinstance(response, bytes):
            response = response.decode("utf-8", errors="replace")
        result[str(address)] = {
            "code": int(code) if code is not None else None,
            "message": str(response),
        }
    return result


def deliver_report(
    *,
    html_content: str,
    run_id: str,
    mode: str,
    artifact_dir,
    delivery_config=None,
    smtp_factory=smtplib.SMTP,
    password=None,
    confirm_live_delivery=False,
    report_html_path=None,
    analysis=None,
    effective_date=None,
    allow_duplicate_effective_date=False,
):
    if mode not in VALID_DELIVERY_MODES:
        raise DeliveryError(f"Unsupported delivery mode: {mode!r}")
    if mode == "disabled":
        return {"state": "disabled", "run_id": run_id}
    if not run_id or not RUN_ID_PATTERN.fullmatch(run_id):
        raise DeliveryError(
            "RUN_ID/PIPELINE_RUN_ID must contain 1-128 safe alphanumeric/._:- "
            "characters for sink and live delivery"
        )

    delivery_config = delivery_config or {}
    if delivery_config.get("enabled") is False:
        return {"state": "disabled", "run_id": run_id, "reason": "config-disabled"}
    if mode == "live" and not confirm_live_delivery:
        raise DeliveryError("Live delivery requires explicit confirmation")
    effective_date = _normalise_effective_date(effective_date)
    subject = _dated_subject(
        delivery_config.get("subject", DEFAULT_LIVE_SUBJECT),
        effective_date,
    )
    sender = delivery_config.get("sender_email")
    recipient = delivery_config.get("recipient_email")
    html_hash = hashlib.sha256(html_content.encode("utf-8")).hexdigest()
    if analysis is None:
        analysis = analyze_recipient_html(html_content)
    elif analysis.html_sha256 != html_hash:
        raise DeliveryError("Recipient HTML analysis does not match email content")
    validate_recipient_html(html_content, analysis=analysis)
    plain_text = build_plain_text_fallback(html_content, analysis=analysis)
    identity = json.dumps(
        {
            "html": html_content,
            "plain_text": plain_text,
            "mime_schema_version": EMAIL_MIME_SCHEMA_VERSION,
            "subject": subject,
            "sender": sender,
            "recipient": recipient,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    content_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    plain_text_hash = hashlib.sha256(plain_text.encode("utf-8")).hexdigest()
    radar_article_count = analysis.radar_article_count
    output_dir = Path(artifact_dir).expanduser().resolve() / "delivery"
    journal_path = output_dir / f"{run_id}.json"
    lock_path = output_dir / f"{run_id}.lock"
    scope_lock_path = None
    if mode == "live" and effective_date:
        scope_lock_path = _daily_delivery_scope_lock(
            artifact_dir,
            recipient=recipient,
            effective_date=effective_date,
        )
    with _delivery_locks(lock_path, scope_lock_path):
        existing = _load_existing_journal(journal_path, content_hash)
        if existing is not None:
            return {**existing, "duplicate": True}
        if scope_lock_path is not None:
            _find_prior_daily_live_delivery(
                artifact_dir,
                current_journal=journal_path,
                recipient=recipient,
                effective_date=effective_date,
                allow_terminal_duplicate=(
                    allow_duplicate_effective_date
                ),
            )

        base = {
            "schema_version": DELIVERY_JOURNAL_SCHEMA_VERSION,
            "mime_schema_version": EMAIL_MIME_SCHEMA_VERSION,
            "delivery_profile": EMAIL_DELIVERY_PROFILE,
            "run_id": run_id,
            "mode": mode,
            "content_sha256": content_hash,
            "html_sha256": html_hash,
            "subject": subject,
            "sender": sender,
            "recipient": recipient,
            "effective_date": effective_date,
            "plain_text_sha256": plain_text_hash,
            "radar_article_count": radar_article_count,
            "duplicate_effective_date_override": bool(
                allow_duplicate_effective_date
            ),
        }
        if mode == "sink":
            html_path = output_dir / f"{run_id}.html"
            _atomic_write(html_path, html_content)
            payload = {**base, "state": "sink", "html_path": str(html_path)}
            _write_journal(journal_path, payload)
            return payload

        if not sender or not recipient:
            raise DeliveryError("Live delivery requires sender_email and recipient_email")
        password = password if password is not None else (
            os.environ.get("SMTP_APP_PASSWORD")
            or os.environ.get("ICLOUD_APP_PASSWORD")
        )
        if not password:
            raise DeliveryError(
                "Live delivery requires SMTP_APP_PASSWORD "
                "(or legacy ICLOUD_APP_PASSWORD)"
            )

        server = delivery_config.get("smtp_server", "smtp.mail.me.com")
        port = int(delivery_config.get("smtp_port", 587))
        smtp_username = (
            delivery_config.get("smtp_username")
            or os.environ.get("SMTP_USERNAME")
            or sender
        )
        message, message_evidence = _build_live_message(
            html_content=html_content,
            subject=subject,
            sender=sender,
            recipient=recipient,
            run_id=run_id,
            analysis=analysis,
        )
        base = {
            **base,
            **message_evidence,
            "message_id": message["Message-ID"],
            "message_date": message["Date"],
            "smtp_server": server,
            "smtp_port": port,
            "smtp_username": smtp_username,
        }
        _write_journal(journal_path, {**base, "state": "pending"})

        smtp = None
        try:
            smtp = smtp_factory(server, port, timeout=20)
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(smtp_username, password)
        except Exception as error:
            _write_journal(
                journal_path,
                {
                    **base,
                    "state": "failed_pre_send",
                    "safe_to_retry": True,
                    "error_type": type(error).__name__,
                },
            )
            if smtp is not None and hasattr(smtp, "close"):
                smtp.close()
            raise DeliveryError(f"Live SMTP setup failed: {error}") from error

        _write_journal(journal_path, {**base, "state": "sending"})
        try:
            refused = smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused as error:
            refused = _serialise_refused_recipients(error.recipients)
            payload = {
                **base,
                "state": "rejected_by_smtp",
                "safe_to_retry": True,
                "smtp_refused_recipients": refused,
            }
            _write_journal(journal_path, payload)
            try:
                smtp.quit()
            except Exception:
                if hasattr(smtp, "close"):
                    smtp.close()
            raise DeliveryError(
                "Live SMTP recipient refused: "
                + ", ".join(sorted(refused))
            ) from error
        except Exception as error:
            # Keep the ambiguous state.  Retrying automatically could duplicate mail.
            raise DeliveryError(f"Live SMTP delivery failed: {error}") from error

        refused = _serialise_refused_recipients(refused)
        if refused:
            payload = {
                **base,
                "state": "rejected_by_smtp",
                "safe_to_retry": True,
                "smtp_refused_recipients": refused,
            }
            _write_journal(journal_path, payload)
            try:
                smtp.quit()
            except Exception:
                if hasattr(smtp, "close"):
                    smtp.close()
            raise DeliveryError(
                "Live SMTP recipient refused: "
                + ", ".join(sorted(refused))
            )

        payload = {
            **base,
            "state": "accepted_by_smtp",
            "safe_to_retry": False,
            "smtp_acceptance": "send_message_returned_no_refusals",
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_journal(journal_path, payload)
        try:
            smtp.quit()
        except Exception:
            # The server has already accepted the message.  A QUIT failure must not
            # turn a confirmed delivery into an ambiguous retry candidate.
            if hasattr(smtp, "close"):
                smtp.close()
        return payload


def build_parser():
    parser = argparse.ArgumentParser(description="Deliver unified report")
    parser.add_argument("--mode", choices=sorted(VALID_DELIVERY_MODES))
    parser.add_argument("--run-id")
    parser.add_argument("--artifact-dir")
    parser.add_argument("--env-file")
    parser.add_argument("--html-file")
    parser.add_argument("--prepared-manifest")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--check-daily-delivery-gate",
        action="store_true",
        help=(
            "Read-only preflight for the recipient/effective-date live-delivery "
            "dedupe gate; performs no report build or SMTP connection"
        ),
    )
    parser.add_argument("--prepared-html")
    parser.add_argument(
        "--prepared-audit-html",
        help="Full unified report retained for database-backed validation",
    )
    parser.add_argument("--expected-html-sha256")
    parser.add_argument(
        "--effective-date",
        help="Logical report date in YYYY-MM-DD; defaults to the pipeline environment",
    )
    parser.add_argument(
        "--confirm-live-delivery",
        action="store_true",
        help="Required explicit acknowledgement for live SMTP delivery",
    )
    parser.add_argument(
        "--allow-duplicate-effective-date-delivery",
        action="store_true",
        help=(
            "Explicitly override the recipient/date live-delivery dedupe gate; "
            "does not bypass same-run idempotency"
        ),
    )
    parser.add_argument(
        "--reconcile-confirmed-delivery",
        action="store_true",
        help=(
            "Mark a sending/accepted_by_smtp journal confirmed_received after "
            "recipient confirmation"
        ),
    )
    parser.add_argument("--expected-recipient")
    parser.add_argument(
        "--confirm-recipient-received",
        action="store_true",
        help="Required acknowledgement that the recipient confirmed receipt",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    _load_runtime_environment(args.env_file)
    if args.check_daily_delivery_gate:
        if args.prepare_only or args.reconcile_confirmed_delivery:
            raise DeliveryError(
                "--check-daily-delivery-gate cannot be combined with report "
                "preparation or delivery reconciliation"
            )
        artifact_dir = (
            args.artifact_dir
            or os.environ.get("PIPELINE_ARTIFACT_DIR")
            or os.environ.get("ARTIFACT_DIR")
        )
        effective_date = (
            args.effective_date
            or os.environ.get("PIPELINE_EFFECTIVE_DATE")
            or os.environ.get("EFFECTIVE_DATE")
        )
        delivery_config = _load_delivery_config()
        result = check_daily_delivery_gate(
            artifact_dir=artifact_dir,
            recipient=delivery_config.get("recipient_email"),
            effective_date=effective_date,
            allow_duplicate_effective_date=(
                args.allow_duplicate_effective_date_delivery
            ),
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    project_root = Path(
        os.environ.get("PROJECT_ROOT")
        or Path(__file__).resolve().parents[1]
    )
    report_html_path = project_root / "reports" / "screening_results.html"
    if args.prepare_only:
        if (
            not args.prepared_html
            or not args.prepared_audit_html
            or not args.prepared_manifest
        ):
            raise DeliveryError(
                "--prepare-only requires --prepared-html, --prepared-audit-html "
                "and --prepared-manifest"
            )
        audit_html = build_unified_html(
            project_root=project_root,
            effective_date=args.effective_date,
        )
        html_content = build_recipient_html(audit_html)
        prepared_path = Path(args.prepared_html).expanduser().resolve()
        audit_path = Path(args.prepared_audit_html).expanduser().resolve()
        manifest_path = Path(args.prepared_manifest).expanduser().resolve()
        digest = hashlib.sha256(html_content.encode("utf-8")).hexdigest()
        audit_digest = hashlib.sha256(audit_html.encode("utf-8")).hexdigest()
        analysis = analyze_recipient_html(html_content)
        radar_article_count = analysis.radar_article_count
        plain_text_sha256 = hashlib.sha256(
            build_plain_text_fallback(html_content, analysis=analysis).encode("utf-8")
        ).hexdigest()
        _atomic_write(prepared_path, html_content)
        _atomic_write(audit_path, audit_html)
        manifest = {
            "schema_version": 2,
            "mime_schema_version": EMAIL_MIME_SCHEMA_VERSION,
            "delivery_profile": EMAIL_DELIVERY_PROFILE,
            "html_path": str(prepared_path),
            "canonical_report_path": str(report_html_path.resolve()),
            "html_sha256": digest,
            "audit_html_path": str(audit_path),
            "audit_html_sha256": audit_digest,
            "radar_article_count": radar_article_count,
            "plain_text_sha256": plain_text_sha256,
            "effective_date": _normalise_effective_date(args.effective_date),
        }
        _atomic_write(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
        )
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        return 0

    mode = args.mode or os.environ.get("DELIVERY_MODE", "disabled")
    run_id = args.run_id or os.environ.get("PIPELINE_RUN_ID") or os.environ.get("RUN_ID")
    artifact_dir = (
        args.artifact_dir
        or os.environ.get("PIPELINE_ARTIFACT_DIR")
        or os.environ.get("ARTIFACT_DIR")
    )
    if mode != "disabled" and not artifact_dir:
        raise DeliveryError("ARTIFACT_DIR/PIPELINE_ARTIFACT_DIR is required")
    if args.reconcile_confirmed_delivery:
        if not artifact_dir:
            raise DeliveryError("ARTIFACT_DIR/PIPELINE_ARTIFACT_DIR is required")
        if not args.confirm_recipient_received:
            raise DeliveryError(
                "Delivery reconciliation requires --confirm-recipient-received"
            )
        result = reconcile_confirmed_delivery(
            artifact_dir=artifact_dir,
            run_id=run_id or "",
            expected_html_sha256=args.expected_html_sha256 or "",
            expected_recipient=args.expected_recipient or "",
            confirmed_by="recipient",
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.expected_html_sha256 and not args.html_file:
        raise DeliveryError("--expected-html-sha256 requires --html-file")
    if args.prepared_manifest and args.html_file:
        raise DeliveryError("--prepared-manifest and --html-file are mutually exclusive")
    if mode == "live" and args.html_file and not args.expected_html_sha256:
        raise DeliveryError(
            "Live delivery of an approved HTML file requires --expected-html-sha256"
        )
    analysis = None
    delivery_effective_date = _normalise_effective_date(
        args.effective_date
        or os.environ.get("PIPELINE_EFFECTIVE_DATE")
        or os.environ.get("EFFECTIVE_DATE")
    )
    if args.prepared_manifest:
        manifest_path = Path(args.prepared_manifest).expanduser().resolve()
        try:
            prepared = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DeliveryError(
                f"Unable to read prepared HTML manifest: {manifest_path}"
            ) from error
        if prepared.get("schema_version") not in {1, 2}:
            raise DeliveryError("Unsupported prepared HTML manifest schema")
        prepared_effective_date = _normalise_effective_date(
            prepared.get("effective_date")
        )
        if (
            delivery_effective_date
            and prepared_effective_date
            and delivery_effective_date != prepared_effective_date
        ):
            raise DeliveryError(
                "Prepared HTML effective date mismatch: "
                f"requested={delivery_effective_date} "
                f"prepared={prepared_effective_date}"
            )
        delivery_effective_date = (
            prepared_effective_date or delivery_effective_date
        )
        expected_mime_schema_version = prepared.get("mime_schema_version")
        if (
            expected_mime_schema_version is not None
            and expected_mime_schema_version != EMAIL_MIME_SCHEMA_VERSION
        ):
            raise DeliveryError(
                "Prepared HTML MIME schema mismatch: "
                f"expected={expected_mime_schema_version} "
                f"supported={EMAIL_MIME_SCHEMA_VERSION}"
            )
        expected_delivery_profile = prepared.get("delivery_profile")
        if expected_delivery_profile != EMAIL_DELIVERY_PROFILE:
            raise DeliveryError(
                "Prepared HTML delivery profile mismatch: "
                f"expected={expected_delivery_profile!r} "
                f"supported={EMAIL_DELIVERY_PROFILE!r}"
            )
        html_content, _ = load_approved_html(
            prepared.get("html_path", ""),
            expected_sha256=prepared.get("html_sha256"),
        )
        analysis = analyze_recipient_html(html_content)
        validate_recipient_html(html_content, analysis=analysis)
        actual_radar_article_count = analysis.radar_article_count
        expected_radar_article_count = prepared.get("radar_article_count")
        if (
            expected_radar_article_count is not None
            and expected_radar_article_count != actual_radar_article_count
        ):
            raise DeliveryError(
                "Prepared HTML radar article count mismatch: "
                f"expected={expected_radar_article_count} "
                f"actual={actual_radar_article_count}"
            )
        expected_plain_text_sha256 = prepared.get("plain_text_sha256")
        if expected_plain_text_sha256 is not None:
            actual_plain_text_sha256 = hashlib.sha256(
                build_plain_text_fallback(
                    html_content, analysis=analysis
                ).encode("utf-8")
            ).hexdigest()
            if expected_plain_text_sha256 != actual_plain_text_sha256:
                raise DeliveryError("Prepared HTML plain-text fallback hash mismatch")
    elif args.html_file:
        html_content, _ = load_approved_html(
            args.html_file, expected_sha256=args.expected_html_sha256
        )
    else:
        html_content = build_unified_html(effective_date=args.effective_date)
    result = deliver_report(
        html_content=html_content,
        run_id=run_id or "",
        mode=mode,
        artifact_dir=artifact_dir or ".",
        delivery_config=_load_delivery_config(),
        confirm_live_delivery=args.confirm_live_delivery,
        analysis=analysis,
        effective_date=delivery_effective_date,
        allow_duplicate_effective_date=(
            args.allow_duplicate_effective_date_delivery
        ),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeliveryError as error:
        print(f"Delivery failed: {error}", file=sys.stderr)
        raise SystemExit(1)
