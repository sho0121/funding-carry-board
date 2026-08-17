#!/usr/bin/env python3
"""
Funding Carry Board ダッシュボード(hyperliquid_funding_dashboard.html)のデータを
再取得し、埋め込み JSON (DATA / SPREAD_DATA) を最新の値に差し替える。

スケジュール実行 (毎時) から呼ばれることを想定:
  1. multi_exchange_arbitrage.build_combined_table() で spot/perp キャリー候補を取得
  2. funding_spread_scanner.build_spread_table() で perp対perp 差分候補を取得
  3. hyperliquid_funding_dashboard.html 内の `const DATA = {...};` と
     `const SPREAD_DATA = {...};` を正規表現で丸ごと置換する
  4. 更新済み HTML を書き戻す (Artifact への再公開は呼び出し側で行う)
"""

import json
import re
import sys
from datetime import datetime, timezone

from multi_exchange_arbitrage import build_combined_table
from funding_spread_scanner import build_spread_table

NOTIONAL_USD = 10000.0
MIN_LIQUIDITY_USD = 20000.0
EXCHANGES = ["hyperliquid", "aster", "backpack", "injective"]
EXCHANGE_LABELS = ["Hyperliquid", "Aster", "Backpack", "Injective"]

HTML_PATH = "hyperliquid_funding_dashboard.html"


def build_carry_payload() -> dict:
    rows, excluded, errors = build_combined_table(NOTIONAL_USD, MIN_LIQUIDITY_USD, EXCHANGES)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "notional_usd": NOTIONAL_USD,
        "min_liquidity_usd": MIN_LIQUIDITY_USD,
        "exchanges": EXCHANGE_LABELS,
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
    rows = build_spread_table(MIN_LIQUIDITY_USD, EXCHANGE_LABELS)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_liquidity_usd": MIN_LIQUIDITY_USD,
        "exchanges": EXCHANGE_LABELS,
        "rows": rows,
    }


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

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"更新完了: {HTML_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
