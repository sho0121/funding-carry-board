#!/usr/bin/env python3
"""
Backpack Exchange の Info API から spot / perps 両方に上場している銘柄一覧を取得する。

GET https://api.backpack.exchange/api/v1/markets が spot・perp 両方の市場を
1本のリストで返す (marketType: "SPOT" | "PERP")。baseSymbol はどちらも同じ
表記で揃っており (Hyperliquid の "Unit" ブリッジ資産のような特殊命名は無い)、
ベースシンボルの完全一致だけで対応関係を判定できる。quote は spot/perp とも
常に USDC。
"""

import json
import sys
import urllib.request

API_URL = "https://api.backpack.exchange"


def fetch(path: str) -> dict:
    req = urllib.request.Request(f"{API_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_markets() -> list[dict]:
    return fetch("/api/v1/markets")


def find_dual_listed() -> list[dict]:
    markets = get_markets()
    spot_by_base = {
        m["baseSymbol"]: m for m in markets if m["marketType"] == "SPOT" and m.get("visible", True)
    }
    perp_by_base = {
        m["baseSymbol"]: m for m in markets if m["marketType"] == "PERP" and m.get("visible", True)
    }

    matched = []
    for base, spot_m in spot_by_base.items():
        perp_m = perp_by_base.get(base)
        if not perp_m:
            continue
        matched.append(
            {
                "perp_symbol": base,
                "perp_contract_symbol": perp_m["symbol"],
                "perp_quote_symbol": perp_m["quoteSymbol"],
                "funding_interval_ms": perp_m["fundingInterval"],
                "spot_base_symbol": base,
                "spot_quote_symbol": spot_m["quoteSymbol"],
                "spot_pair_name": spot_m["symbol"],
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
