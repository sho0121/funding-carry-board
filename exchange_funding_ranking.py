#!/usr/bin/env python3
"""
取引所別ファンディングレートランキング。

risk_manager.py の「リスク調整後ランキング」(裁定ペアのcomposite_scoreによる
ランキング)とは別物 ── こちらは単純に「今どの取引所のどの銘柄のfundingレートが
高い(または低い)か」を銘柄単位でそのまま見せる生データビュー。プラス・マイナス
両方の絶対値上位を表示する(マイナスのfundingはロング側が受け取れる機会を示す)。

表示名について: Injectiveのデータ取得元(Indexer API)には「Helix採用済みか」を示す
フラグが無く、チェーン上の全市場を無差別に返す。このモジュールは実際の約定履歴で
出来高を検証した銘柄だけを残しており、これは実質的に「Helixで取引可能な銘柄」への
最も確度の高い近似(実機検証済み: AR/USDC PERPはこの検証で弾かれ、実際にHelixの
検索にも出てこないことを確認済み)。そのため表示名は "Injective" ではなく "Helix"
としている(データ取得元のAPI自体は変わらず、Helix専用の別APIが存在するわけではない
点に注意)。この事情から、他の取引所より検証範囲を広めに取っている。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from funding_spread_scanner import FETCHERS, fetch_injective_24h_volume_usd

MIN_LIQUIDITY_USD = 20000.0
TOP_N = 15
HELIX_VERIFY_TOP_N = 40  # Helix名義で表示するため、他取引所より広めに実出来高を検証する

DISPLAY_NAME_OVERRIDES = {"Injective": "Helix"}


def _rank_exchange(exchange: str, rows: list[dict]) -> list[dict]:
    """funding_apr_pctの絶対値が大きい順(プラス・マイナス両方)に上位を返す。"""
    nonzero = [r for r in rows if r["funding_apr_pct"] != 0]
    nonzero.sort(key=lambda r: abs(r["funding_apr_pct"]), reverse=True)

    if exchange == "Injective":
        verified = []
        for r in nonzero[:HELIX_VERIFY_TOP_N]:
            volume, complete = fetch_injective_24h_volume_usd(r["contract_symbol"])
            if volume is not None and complete and volume >= MIN_LIQUIDITY_USD:
                verified.append({**r, "volume_24h_usd": round(volume, 2)})
        verified.sort(key=lambda r: abs(r["funding_apr_pct"]), reverse=True)
        return verified[:TOP_N]

    liquid = [
        r for r in nonzero if r["volume_24h_usd"] is not None and r["volume_24h_usd"] >= MIN_LIQUIDITY_USD
    ]
    return liquid[:TOP_N]


def build_exchange_ranking_payload() -> dict:
    exchanges_data = {}
    for exchange, fetcher in FETCHERS.items():
        display_name = DISPLAY_NAME_OVERRIDES.get(exchange, exchange)
        try:
            rows = fetcher()
        except Exception as e:
            exchanges_data[display_name] = {"error": str(e), "total_nonzero": 0, "top": []}
            continue

        top = _rank_exchange(exchange, rows)
        exchanges_data[display_name] = {
            "total_nonzero": sum(1 for r in rows if r["funding_apr_pct"] != 0),
            "top": [
                {
                    "base_symbol": r["base_symbol"],
                    "funding_apr_pct": round(r["funding_apr_pct"], 4),
                    "mark_price": r["mark_price"],
                    "volume_24h_usd": r["volume_24h_usd"],
                }
                for r in top
            ],
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_liquidity_usd": MIN_LIQUIDITY_USD,
        "helix_verify_top_n": HELIX_VERIFY_TOP_N,
        "exchanges": exchanges_data,
    }


def main():
    parser = argparse.ArgumentParser(description="取引所別ファンディングレートランキングを生成する")
    parser.add_argument("-o", "--output", default="exchange_funding_ranking.json")
    args = parser.parse_args()

    payload = build_exchange_ranking_payload()
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    for exchange, data in payload["exchanges"].items():
        if "error" in data:
            print(f"{exchange}: 取得失敗 ({data['error']})", file=sys.stderr)
            continue
        print(f"{exchange}: 非ゼロ{data['total_nonzero']}件中、上位{len(data['top'])}件を検証済み表示", file=sys.stderr)
        for r in data["top"][:5]:
            print(f"  {r['base_symbol']} APR {r['funding_apr_pct']:.2f}%", file=sys.stderr)


if __name__ == "__main__":
    main()
