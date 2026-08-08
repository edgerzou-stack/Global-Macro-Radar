import json
import os

from hotspot_evidence import publish_hotspot_evidence
from pipeline_selection import select_report_articles
from run_date import logical_today


def _evidence_text(article):
    tier = str(article.get("source_tier") or "unclassified")
    state = str(article.get("evidence_state") or "discovery_only")
    trade = "trade-evidence" if article.get("trade_evidence_eligible") else "research-only"
    return f"{tier} · {state} · {trade}"


def _write_article_block(handle, article):
    score = article["score_data"]
    title = score.get("translated_title", article["title"])
    handle.write(
        "### "
        f"[硬核:{float(score.get('innovation_score', 0)):.2f} | "
        f"流量:{float(score.get('traffic_score', 0)):.2f}] {title}\n"
    )
    if title != article["title"] and score.get("translated_title"):
        handle.write(f"*{article['title']}*\n\n")
    handle.write(
        f"**来源**: {article['source']} | "
        f"**日期**: {article['published_at'][:10]}\n\n"
    )
    handle.write(f"**证据等级**: {_evidence_text(article)}\n\n")
    if article.get("strategic_topic") not in {None, "unrelated"}:
        handle.write(
            "**产业里程碑**: "
            f"{article.get('strategic_topic')} / "
            f"{article.get('industrial_milestone')} / "
            f"{article.get('production_state')}\n\n"
        )
    if score.get("translated_summary"):
        handle.write(f"**摘要**: {score['translated_summary']}\n\n")
    handle.write(f"> **点评**: {score['justification']}\n\n")
    handle.write(f"[阅读原文]({article['link']})\n\n---\n")


def generate_markdown_report(
    scored_articles,
    config,
    output_dir=None,
    *,
    deduplicate=True,
):
    from score import (
        apply_industry_relevance_gate,
        deduplicate_articles,
    )

    output_dir = output_dir or os.environ.get(
        "RADAR_REPORTS_DIR",
        "reports",
    )
    os.makedirs(output_dir, exist_ok=True)
    report_date = logical_today()
    report_path = os.path.join(
        output_dir,
        f"industry_report_{report_date.isoformat()}.md",
    )

    def deduplicator(items, selection_config):
        print(
            f"Deduplicating {len(items)} high-scoring articles...",
            flush=True,
        )
        result = deduplicate_articles(items, selection_config)
        print(
            f"After deduplication: {len(result)} articles remaining.",
            flush=True,
        )
        return result

    selection = select_report_articles(
        scored_articles,
        config,
        report_date,
        deduplicate=deduplicate,
        relevance_gate=apply_industry_relevance_gate,
        deduplicator=deduplicator,
    )
    diagnostics = selection.diagnostics
    print(
        "RADAR_SELECTION_SUMMARY "
        + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True),
        flush=True,
    )
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(
            "# 科技产业情报雷达 - Daily Report "
            f"({report_date.isoformat()})\n\n"
        )
        handle.write(
            "> **选题覆盖**："
            f"双高 {diagnostics['supernova']} · "
            f"硬核 {diagnostics['hardcore']} · "
            f"流量 {diagnostics['hype']} · "
            f"战略追踪 {diagnostics['strategic_watch']}"
        )
        if diagnostics["near_hardcore"]:
            handle.write(
                f" · 硬核近门槛 {diagnostics['near_hardcore']}"
            )
        handle.write("\n\n")
        concentration_threshold = float(
            config.get("output", {}).get(
                "report_source_concentration_warning_ratio",
                0.6,
            )
        )
        if (
            diagnostics["selected"] >= 3
            and diagnostics["leading_source_share"]
            >= concentration_threshold
        ):
            handle.write(
                "> ⚠️ **来源集中提示**："
                f"{diagnostics['leading_source']} 占本期入选新闻 "
                f"{diagnostics['leading_source_share']:.0%}；"
                "请结合其他独立信源交叉验证。\n\n"
            )
        evidence_warning_ratio = float(
            config.get("output", {}).get(
                "report_min_primary_supported_ratio",
                0.7,
            )
        )
        if (
            diagnostics["selected"]
            and diagnostics["primary_supported_ratio"] < evidence_warning_ratio
        ):
            handle.write(
                "> ⚠️ **一手证据不足**："
                f"本期高分事件仅 {diagnostics['primary_supported_ratio']:.0%} "
                "具备 T0/T1 或已核验官方原文；其余仅作为研究线索，"
                "不得直接驱动交易。\n\n"
            )
        if not (
            selection.supernova
            or selection.hardcore
            or selection.hype
            or selection.strategic_watch
        ):
            handle.write(
                "今天没有任何新闻达到你设置的超高标准 "
                "(全板块 8 分以下)。\n\n"
                "_真正的结构性大机会不会每天都有，"
                "享受这片刻的宁静吧。_\n"
            )

        if selection.deep_dives:
            handle.write(
                "## 🤿 深度研报 (Deep Dive)\n"
                "_系统已自动溯源第一手官方资料，"
                "由 AI 生成顶尖研报。_\n\n"
            )
            for article in selection.deep_dives:
                score = article["score_data"]
                title = score.get(
                    "translated_title",
                    article["title"],
                )
                deep_dive = article["deep_dive"]
                handle.write(
                    "### "
                    f"[硬核:{float(score.get('innovation_score', 0)):.2f} "
                    f"| 流量:{float(score.get('traffic_score', 0)):.2f}] "
                    f"{title}\n"
                )
                if (
                    title != article["title"]
                    and score.get("translated_title")
                ):
                    handle.write(f"*{article['title']}*\n\n")
                handle.write(
                    f"**来源**: {article['source']} | "
                    f"**日期**: {article['published_at'][:10]}\n\n"
                )
                if score.get("translated_summary"):
                    handle.write(
                        f"**摘要**: {score['translated_summary']}\n\n"
                    )
                handle.write(
                    f"> **点评**: {score['justification']}\n\n"
                )
                handle.write(
                    f"[🌐 溯源官方原文]({deep_dive['primary_url']})\n\n"
                )
                handle.write(
                    '<details markdown="1" style="margin-top: 15px; '
                    'margin-bottom: 20px;">\n'
                )
                handle.write(
                    '  <summary style="cursor: pointer; color: #3b82f6; '
                    'font-weight: bold; font-size: 16px;">'
                    "👇 点击展开/收起 AI 深度研报全文</summary>\n"
                )
                handle.write(
                    '  <div markdown="1" style="margin-top: 15px; '
                    "padding: 20px; background: #f8fafc; "
                    "border-radius: 8px; border-left: 4px solid #3b82f6; "
                    'font-size: 14px; line-height: 1.6;">\n\n'
                )
                handle.write(f"**{title} - 深度研报**\n\n")
                handle.write(f"{deep_dive['report_content']}\n\n")
                handle.write("  </div>\n</details>\n\n---\n")

        if selection.strategic_watch:
            handle.write(
                "## 🧭 战略硬科技追踪 (Research Watch)\n"
                "_具体技术里程碑已出现但尚未达到主榜阈值；"
                "默认仅供研究，不直接驱动热点交易。_\n\n"
            )
            for article in selection.strategic_watch:
                _write_article_block(handle, article)

        if selection.supernova:
            handle.write(
                "## 🌟 顶流硬核 (Supernova)\n"
                "_兼具颠覆性技术价值与爆炸性市场流量的里程碑事件！"
                "_\n\n"
            )
            for article in selection.supernova:
                _write_article_block(handle, article)
        if selection.hardcore:
            handle.write(
                "## 🔬 科技硬核创新 (Hardcore Innovation)\n"
                "_改变世界的底层力量。也许目前大众尚未狂热，"
                "但具有长远商业价值。_\n\n"
            )
            for article in selection.hardcore:
                _write_article_block(handle, article)
        if selection.hype:
            handle.write(
                "## 📈 产业焦点与流量狂欢 (Traffic & Hype)\n"
                "_当前资本和大众的注意力焦点。可能是风口，"
                "也可能是抓马泡沫。_\n\n"
            )
            for article in selection.hype:
                _write_article_block(handle, article)
    evidence_articles = (
        list(selection.strategic_watch)
        + list(selection.supernova)
        + list(selection.hardcore)
        + list(selection.hype)
    )
    publish_hotspot_evidence(
        report_path,
        evidence_articles,
        report_date.isoformat(),
    )
    return report_path
