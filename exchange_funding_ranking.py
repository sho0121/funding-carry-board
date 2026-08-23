#!/usr/bin/env python3
"""
取引所別ファンディングレートランキング。

risk_manager.py の「リスク調整後ランキング」(裁定ペアのcomposite_scoreによる
ランキング)とは別物 ── こちらは単純に「今どの取引所のどの銘柄のfundingレートが
高いか」を銘柄単位でそのまま見せる生データビュー。

Hyperliquid/Aster/Backpack/edgeXは取得時点で24h出来高が判明しているためそのまま
最小流動性でフィルタできるが、Injectiveは瞬間値取得時点では出来高が不明なため、
瞬間APR上位 INJECTIVE_VERIFY_TOP_N 件だけ実際の約定履歴から出来高を検証する
(全銘柄検証は既にfunding_spread_scanner.pyの差分スキャナーが行っており、ここで
重複して全件検証するのはコストが高すぎるため)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from funding_spread_scanner import FETCHERS, fetch_injective_24h_volume_usd

MIN_LIQUIDITY_USD = 20000.0
TOP_N = 15
INJECTIVE_VERIFY_TOP_N = 20


def _rank_exchange(exchange: str, rows: list[dict]) -> list[dict]:
    positive = [r for r in rows if r["funding_apr_pct"] > 0]
    positive.sort(key=lambda r: r["funding_apr_pct"], reverse=True)

    if exchange == "Injective":
        verified = []
        for r in positive[:INJECTIVE_VERIFY_TOP_N]:
            volume, complete = fetch_injective_24h_volume_usd(r["contract_symbol"])
            if volume is not None and complete and volume >= MIN_LIQUIDITY_USD:
                verified.append({**r, "volume_24h_usd": round(volume, 2)})
        verified.sort(key=lambda r: r["funding_apr_pct"], reverse=True)
        return verified[:TOP_N]

    liquid = [
        r for r in positive if r["volume_24h_usd"] is not None and r["volume_24h_usd"] >= MIN_LIQUIDITY_USD
    ]
    return liquid[:TOP_N]


def build_exchange_ranking_payload() -> dict:
    exchanges_data = {}
    for exchange, fetcher in FETCHERS.items():
        try:
            rows = fetcher()
        except Exception as e:
            exchanges_data[exchange] = {"error": str(e), "total_positive": 0, "top": []}
            continue

        top = _rank_exchange(exchange, rows)
        exchanges_data[exchange] = {
            "total_positive": sum(1 for r in rows if r["funding_apr_pct"] > 0),
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
        "injective_verify_top_n": INJECTIVE_VERIFY_TOP_N,
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
        print(f"{exchange}: プラス{data['total_positive']}件中、上位{len(data['top'])}件を検証済み表示", file=sys.stderr)
        for r in data["top"][:5]:
            print(f"  {r['base_symbol']} APR {r['funding_apr_pct']:.2f}%", file=sys.stderr)


if __name__ == "__main__":
    main()
