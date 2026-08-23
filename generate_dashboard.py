#!/usr/bin/env python3
"""
Funding Carry Board ダッシュボード(hyperliquid_funding_dashboard.html)のデータを
再取得し、埋め込み JSON (DATA / SPREAD_DATA / RANKING_DATA / INTEL_DATA / TEAM_DATA /
PAPER_BOT_DATA / EDGE_DATA / EXCHANGE_RANKING_DATA) を最新の値に差し替える。

スケジュール実行 (毎時) から呼ばれることを想定:
  1. multi_exchange_arbitrage.build_combined_table() で spot/perp キャリー候補を取得
  2. funding_spread_scanner.build_spread_table() で perp対perp 差分候補を取得
  3. risk_manager.score_and_rank() で 1・2 を横断したリスク調整後ランキングを算出
  4. market_intel.fetch_market_intel() でリスクイベント・トレンド銘柄を取得
  5. build_team_status_payload() で上記1〜4の結果からAI社員パネル用データを組み立てる
     (portfolio.py/positions.json には一切アクセスしない ── 実運用金額を含む個人情報を
     毎時自動公開されるこのダッシュボードに載せないため)
  6. paper_bot.run_cycle() で1・2の行データをそのまま使い、ペーパートレードBotの
     エグジット/エントリー判定を1サイクル実行する(実弾なし。paper_positions.json は
     シミュレーションのみで実金額を含まないため公開・git管理してよい)
  7. edge_watch.fetch_edge_signals() で「エッジ・ラボ」事業部の自動監視(新規上場検知・
     異常ベーシス検知)を実行し、edge_playbook.md の検証状況サマリーも添える
  8. exchange_funding_ranking.build_exchange_ranking_payload() で取引所別の生の
     ファンディングレートランキング(risk_manager.pyの裁定ペアランキングとは別物)を算出
  9. hyperliquid_funding_dashboard.html 内の `const DATA = {...};` 等8つの定数を
     正規表現で丸ごと置換する
  10. 更新済み HTML を書き戻す (Artifact への再公開は呼び出し側で行う)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

from multi_exchange_arbitrage import build_combined_table
from funding_spread_scanner import build_spread_table
from risk_manager import TOTAL_CAPITAL_USD, score_and_rank
from market_intel import fetch_market_intel
from paper_bot import run_cycle as run_paper_bot_cycle
from exchange_funding_ranking import build_exchange_ranking_payload
from edge_watch import fetch_edge_signals

EDGE_PLAYBOOK_PATH = "edge_playbook.md"
EDGE_PLAYBOOK_STATUSES = ["自社データで検証済み", "文献ベース", "未検証", "検証不可(データ基盤なし)"]

NOTIONAL_USD = 10000.0
MIN_LIQUIDITY_USD = 20000.0
# キャリー(spot+perp)側はspot取引が無い取引所を含められない
CARRY_EXCHANGES = ["hyperliquid", "aster", "backpack", "injective"]
CARRY_EXCHANGE_LABELS = ["Hyperliquid", "Aster", "Backpack", "Injective"]
# 差分(perp対perp)側はperpのみの取引所(edgeX)も対象にできる
SPREAD_EXCHANGE_LABELS = ["Hyperliquid", "Aster", "Backpack", "Injective", "edgeX", "dYdX", "ApeX"]

HTML_PATH = "hyperliquid_funding_dashboard.html"


def build_carry_payload() -> dict:
    rows, excluded, errors = build_combined_table(NOTIONAL_USD, MIN_LIQUIDITY_USD, CARRY_EXCHANGES)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "notional_usd": NOTIONAL_USD,
        "min_liquidity_usd": MIN_LIQUIDITY_USD,
        "exchanges": CARRY_EXCHANGE_LABELS,
        "rows": rows,
        "excluded": [
            {
                "exchange": e["exchange"],
                "perp_symbol": e["perp_symbol"],
                "spot_pair_name": e["spot_pair_name"],
                "match_type": e["match_type"],
                "spot_volume_usd": e["spot_volume_usd"],
            }
            for e in excluded
        ],
        "errors": errors,
    }


def build_spread_payload() -> dict:
    rows, excluded = build_spread_table(MIN_LIQUIDITY_USD, SPREAD_EXCHANGE_LABELS)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_liquidity_usd": MIN_LIQUIDITY_USD,
        "exchanges": SPREAD_EXCHANGE_LABELS,
        "rows": rows,
        "excluded": [
            {
                "base_symbol": e["base_symbol"],
                "short_exchange": e["short_exchange"],
                "long_exchange": e["long_exchange"],
                "short_volume_24h_usd": e["short_volume_24h_usd"],
                "long_volume_24h_usd": e["long_volume_24h_usd"],
            }
            for e in excluded
        ],
    }


def build_ranking_payload(carry_rows: list, spread_rows: list) -> dict:
    ranked = score_and_rank(carry_rows, spread_rows, TOTAL_CAPITAL_USD)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capital_usd": TOTAL_CAPITAL_USD,
        "rows": ranked,
    }


def build_intel_payload() -> dict:
    return fetch_market_intel()


def build_team_status_payload(
    carry_payload: dict, spread_payload: dict, ranking_payload: dict, intel_payload: dict
) -> dict:
    """AI社員(サブエージェント)5体の稼働状況パネル用データ。
    portfolio-manager は positions.json (実際の運用金額を含む個人情報) を一切参照しない
    ── ダッシュボードは毎時GitHub Actionsで自動公開されるため、意図的にここでは
    ファイルの存在確認すら行わない。"""
    latest_report = None
    if os.path.isdir("reports"):
        report_files = sorted(f for f in os.listdir("reports") if f.endswith(".md"))
        if report_files:
            latest_report = report_files[-1].removesuffix(".md")

    top = ranking_payload["rows"][0] if ranking_payload["rows"] else None
    intel_events = [e for e in intel_payload.get("risk_events", []) if "error" not in e]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "members": [
            {
                "role": "research-analyst",
                "label": "リサーチ担当",
                "icon": "🔍",
                "status": f"{len(carry_payload['rows']) + len(spread_payload['rows'])}件の裁定候補を検出",
                "detail": f"キャリー{len(carry_payload['rows'])}件 / 差分{len(spread_payload['rows'])}件",
            },
            {
                "role": "market-intel-analyst",
                "label": "市場インテリジェンス担当",
                "icon": "📰",
                "status": f"リスクイベント{len(intel_events)}件を監視中",
                "detail": "定性メモあり" if intel_payload.get("analyst_notes_available") else "定性メモなし(オンデマンドで依頼可)",
            },
            {
                "role": "risk-manager",
                "label": "リスク管理担当",
                "icon": "🛡️",
                "status": f"{len(ranking_payload['rows'])}件を評価しランキング済み",
                "detail": f"総合1位: {top['label']}" if top else "評価対象なし",
            },
            {
                "role": "ops-reporter",
                "label": "運用管理担当",
                "icon": "📋",
                "status": f"最新日次レポート: {latest_report}" if latest_report else "日次レポート未生成",
                "detail": "ダッシュボードは毎時自動更新",
            },
            {
                "role": "portfolio-manager",
                "label": "収益管理担当",
                "icon": "💰",
                "status": "ローカル専用(このダッシュボードには非表示)",
                "detail": "positions.jsonは実運用金額を含むためgit管理・公開対象外",
            },
        ],
    }


def _summarize_edge_playbook(path: str = EDGE_PLAYBOOK_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    return {status: text.count(f"**{status}**") for status in EDGE_PLAYBOOK_STATUSES}


def build_edge_payload(carry_rows: list) -> dict:
    payload = fetch_edge_signals(carry_rows)
    payload["playbook_summary"] = _summarize_edge_playbook()
    return payload


def inject(html: str, var_name: str, payload: dict) -> str:
    new_line = f"const {var_name} = " + json.dumps(payload, ensure_ascii=False) + ";"
    pattern = rf"const {var_name} = \{{.*?\}};"
    updated, count = re.subn(pattern, lambda m: new_line, html, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{var_name} の置換に失敗しました (見つかった箇所: {count})")
    return updated


def main():
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    print("carry データ取得中...", file=sys.stderr)
    carry_payload = build_carry_payload()
    html = inject(html, "DATA", carry_payload)
    print(f"  -> {len(carry_payload['rows'])} 件", file=sys.stderr)

    print("spread データ取得中...", file=sys.stderr)
    spread_payload = build_spread_payload()
    html = inject(html, "SPREAD_DATA", spread_payload)
    print(f"  -> {len(spread_payload['rows'])} 件", file=sys.stderr)

    print("リスク調整後ランキング算出中...", file=sys.stderr)
    ranking_payload = build_ranking_payload(carry_payload["rows"], spread_payload["rows"])
    html = inject(html, "RANKING_DATA", ranking_payload)
    print(f"  -> {len(ranking_payload['rows'])} 件", file=sys.stderr)

    print("市場インテリジェンス取得中...", file=sys.stderr)
    intel_payload = build_intel_payload()
    html = inject(html, "INTEL_DATA", intel_payload)
    print(
        f"  -> リスクイベント {len(intel_payload['risk_events'])} 件 / "
        f"トレンド銘柄 {len(intel_payload['trending_coins'])} 件",
        file=sys.stderr,
    )

    team_status_payload = build_team_status_payload(
        carry_payload, spread_payload, ranking_payload, intel_payload
    )
    html = inject(html, "TEAM_DATA", team_status_payload)

    print("ペーパートレードBot サイクル実行中...", file=sys.stderr)
    paper_bot_payload = run_paper_bot_cycle(carry_payload["rows"], spread_payload["rows"], TOTAL_CAPITAL_USD)
    html = inject(html, "PAPER_BOT_DATA", paper_bot_payload)
    print(
        f"  -> オープン{paper_bot_payload['open_count']}件 / "
        f"確定損益${paper_bot_payload['realized_pnl_usd']:,.2f}",
        file=sys.stderr,
    )

    print("エッジ・ラボ シグナル取得中...", file=sys.stderr)
    edge_payload = build_edge_payload(carry_payload["rows"])
    html = inject(html, "EDGE_DATA", edge_payload)
    print(
        f"  -> 新規上場{len(edge_payload['new_listings'])}件 / "
        f"異常ベーシス{len(edge_payload['extreme_basis'])}件",
        file=sys.stderr,
    )

    print("取引所別ファンディングレートランキング算出中...", file=sys.stderr)
    exchange_ranking_payload = build_exchange_ranking_payload()
    html = inject(html, "EXCHANGE_RANKING_DATA", exchange_ranking_payload)
    for exchange, data in exchange_ranking_payload["exchanges"].items():
        print(f"  -> {exchange}: {len(data.get('top', []))}件", file=sys.stderr)

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"更新完了: {HTML_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
