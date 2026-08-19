#!/usr/bin/env python3
"""
risk_manager.py のランキング結果(+ market_intel.py の市場インテリジェンス)を読み込み、
その日の推奨裁定・根拠・警告事項を Markdown でまとめる (reports/YYYY-MM-DD.md)。

Slack/メール等への自動送信は行わない(送信操作は明示的許可が必要な行為のため)。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone

RISK_CSV_DEFAULT = "risk_assessed_opportunities.csv"
INTEL_JSON_DEFAULT = "market_intel.json"
REPORTS_DIR_DEFAULT = "reports"
TOP_N = 10


def _read_ranked_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_intel_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def build_report(ranked_rows: list[dict], intel: dict | None) -> str:
    now = datetime.now(timezone.utc)
    lines = [f"# Funding Carry Board 日次レポート ({now.strftime('%Y-%m-%d')})", ""]
    lines.append(f"生成日時(UTC): {now.isoformat()}")
    lines.append("")

    lines.append("## 今日の推奨裁定 (上位候補)")
    lines.append("")
    lines.append("| # | 銘柄 | タイプ | APR | 推奨サイズ | 想定年間利益 | 注意事項 |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in ranked_rows[:TOP_N]:
        flags = row.get("risk_flags") or "-"
        lines.append(
            f"| {row['rank']} | {row['base_symbol']} | {row['opportunity_type']} | "
            f"{float(row['apr_pct']):.2f}% | ${float(row['recommended_position_usd']):,.0f} | "
            f"${float(row['est_annual_profit_usd']):,.0f} | {flags} |"
        )
    lines.append("")

    allocated = sum(float(r["recommended_position_usd"]) for r in ranked_rows)
    total_profit = sum(float(r["est_annual_profit_usd"]) for r in ranked_rows)
    lines.append(f"- 推奨配分合計: ${allocated:,.0f}")
    lines.append(f"- 想定年間利益合計: ${total_profit:,.0f}")
    lines.append(f"- 評価した候補数: {len(ranked_rows)}件")
    lines.append("")

    lines.append("## 市場インテリジェンス")
    lines.append("")
    if intel is None:
        lines.append("(market_intel.json が見つかりません。先に `python3 market_intel.py` を実行してください)")
    else:
        events = [e for e in intel.get("risk_events", []) if "error" not in e]
        if events:
            lines.append(f"直近{intel.get('lookback_days', '?')}日間のリスクイベント: {len(events)}件")
            for e in events:
                lines.append(
                    f"- [{e['exchange']}] {e['name']} ({e['date']}, {e.get('classification', '-')}, "
                    f"被害額 ${e.get('amount_usd') or 0:,.0f})"
                )
        else:
            lines.append("対象取引所名と完全一致するリスクイベントはありません。")
        lines.append("")
        trending = [c for c in intel.get("trending_coins", []) if "error" not in c][:5]
        if trending:
            lines.append("トレンド銘柄(上位5件): " + ", ".join(c["symbol"] for c in trending))
        if intel.get("analyst_notes_available"):
            lines.append("")
            lines.append("market_intel_notes.md に market-intel-analyst の定性メモあり(推奨ランキングに反映済み)")

    lines.append("")
    lines.append("---")
    lines.append("投資助言ではありません。執行前に自分で板の厚み・手数料・清算リスクを確認してください。")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="日次レポート(Markdown)を生成する")
    parser.add_argument("--risk-csv", default=RISK_CSV_DEFAULT)
    parser.add_argument("--intel-json", default=INTEL_JSON_DEFAULT)
    parser.add_argument("--reports-dir", default=REPORTS_DIR_DEFAULT)
    args = parser.parse_args()

    ranked_rows = _read_ranked_csv(args.risk_csv)
    intel = _read_intel_json(args.intel_json)

    report = build_report(ranked_rows, intel)

    os.makedirs(args.reports_dir, exist_ok=True)
    out_path = os.path.join(args.reports_dir, f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"日次レポートを生成しました -> {out_path}")


if __name__ == "__main__":
    main()
