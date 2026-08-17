#!/usr/bin/env python3
"""
Aster (asterdex.com) の Info API から perps と spot の両方に上場している銘柄一覧を取得する。

Aster は Binance ライクな REST API を持ち、futures 側 (fapi.asterdex.com) の
baseAsset と spot 側 (sapi.asterdex.com) の baseAsset が同じ文字列で揃っている
ため (Hyperliquid の "Unit" ブリッジ資産のような特殊な命名規則は無い)、
ベースシンボルの完全一致だけで対応関係を判定できる。

- GET https://fapi.asterdex.com/fapi/v1/exchangeInfo -> perps の symbols (baseAsset/quoteAsset/status)
- GET https://sapi.asterdex.com/api/v1/exchangeInfo  -> spot  の symbols (baseAsset/quoteAsset/status)

spot 側は同じベースシンボルに対して複数 quote (USDT/USD1/...) のペアが存在する
ことがあるため、実際の出来高比較は呼び出し側 (aster_funding_arbitrage.py) で行う。
"""

import json
import sys
import urllib.request

FAPI_URL = "https://fapi.asterdex.com"
SAPI_URL = "https://sapi.asterdex.com"


def fetch(base_url: str, path: str) -> dict:
    req = urllib.request.Request(f"{base_url}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_perp_contract_by_base() -> dict[str, dict]:
    """ベースシンボルごとの perp 契約を返す。USDT建てを優先し、無ければ最初の契約を使う"""
    info = fetch(FAPI_URL, "/fapi/v1/exchangeInfo")
    by_base: dict[str, list[dict]] = {}
    for s in info["symbols"]:
        if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL":
            continue
        by_base.setdefault(s["baseAsset"], []).append(
            {"contract_symbol": s["symbol"], "quote_symbol": s["quoteAsset"]}
        )

    chosen = {}
    for base, contracts in by_base.items():
        usdt = next((c for c in contracts if c["quote_symbol"] == "USDT"), None)
        chosen[base] = usdt or contracts[0]
    return chosen


def get_spot_pairs() -> list[dict]:
    info = fetch(SAPI_URL, "/api/v1/exchangeInfo")
    pairs = []
    for s in info["symbols"]:
        if s.get("status") != "TRADING":
            continue
        pairs.append(
            {
                "base_symbol": s["baseAsset"],
                "quote_symbol": s["quoteAsset"],
                "spot_symbol": s["symbol"],
            }
        )
    return pairs


def find_dual_listed() -> list[dict]:
    perp_by_base = get_perp_contract_by_base()
    spot_pairs = get_spot_pairs()

    matched = []
    for pair in spot_pairs:
        contract = perp_by_base.get(pair["base_symbol"])
        if contract:
            matched.append(
                {
                    "perp_symbol": pair["base_symbol"],
                    "perp_contract_symbol": contract["contract_symbol"],
                    "perp_quote_symbol": contract["quote_symbol"],
                    "spot_base_symbol": pair["base_symbol"],
                    "spot_quote_symbol": pair["quote_symbol"],
                    "spot_pair_name": pair["spot_symbol"],
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
