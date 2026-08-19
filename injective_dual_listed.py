#!/usr/bin/env python3
"""
Injective (Helix) の Indexer API から spot / perps 両方に上場している銘柄一覧を取得する。

GET https://sentry.exchange.grpc-web.injective.network/api/exchange/spot/v1/markets
GET https://sentry.exchange.grpc-web.injective.network/api/exchange/derivative/v1/markets

ticker は spot が "BASE/QUOTE"、perp が "BASE/QUOTE PERP" の形式。
ベースシンボルの完全一致で対応関係を判定する。

注意: Injective の Indexer は gRPC-Web ベースで、価格・24h出来高・funding履歴を
返す REST エンドポイント (旧 chronos サービス) が現在たどれなかった
(候補ホストが 404/503)。そのため本モジュールはマッチングのみを担当し、
価格・出来高は取得しない (injective_funding_arbitrage.py 側で
funding rate のみを使った簡易実装としている)。
"""

import json
import sys
import urllib.request

API_URL = "https://sentry.exchange.grpc-web.injective.network"


def fetch(path: str) -> dict:
    req = urllib.request.Request(
        f"{API_URL}{path}", headers={"User-Agent": "Mozilla/5.0"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_spot_markets() -> list[dict]:
    return fetch("/api/exchange/spot/v1/markets")["markets"]


def get_derivative_markets() -> list[dict]:
    return fetch("/api/exchange/derivative/v1/markets")["markets"]


def find_dual_listed() -> list[dict]:
    spot_markets = [m for m in get_spot_markets() if m["marketStatus"] == "active"]
    perp_markets = [
        m
        for m in get_derivative_markets()
        if m["marketStatus"] == "active" and m.get("isPerpetual")
    ]

    spot_by_base = {m["ticker"].split("/")[0]: m for m in spot_markets}
    perp_by_base = {m["ticker"].split("/")[0]: m for m in perp_markets}

    matched = []
    for base, spot_m in spot_by_base.items():
        perp_m = perp_by_base.get(base)
        if not perp_m:
            continue
        funding_info = perp_m.get("perpetualMarketInfo") or {}
        matched.append(
            {
                "perp_symbol": base,
                "perp_market_id": perp_m["marketId"],
                "perp_ticker": perp_m["ticker"],
                "funding_interval_seconds": funding_info.get("fundingInterval"),
                "spot_market_id": spot_m["marketId"],
                "spot_ticker": spot_m["ticker"],
                "spot_quote_decimals": spot_m["quoteTokenMeta"]["decimals"],
                "match_type": "exact",
            }
        )
    matched.sort(key=lambda r: r["perp_symbol"])
    return matched


def main():
    matched = find_dual_listed()
    json.dump(matched, sys.stdout, ensure_ascii=False, indent=2)
    print(f"\n{len(matched)} 件の spot ペアが perps と対応しています", file=sys.stderr)


if __name__ == "__main__":
    main()
