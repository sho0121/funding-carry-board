#!/usr/bin/env python3
"""
Backpack Exchange で spot / perps 両方に上場している銘柄について、
ファンディングレート・アービトラージ候補を一覧化する。
出力スキーマは hyperliquid_funding_arbitrage.py / aster_funding_arbitrage.py と共通で、
"exchange" フィールドで取引所を区別する。

Backpack の funding 間隔は現時点で全 perp が1時間固定だが、market メタデータの
fundingInterval (ms) をそのまま使い、固定値を仮定しないようにしている。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from backpack_dual_listed import API_URL, fetch, find_dual_listed  # noqa: E402

FUNDING_HISTORY_LOOKBACK_MS = 3 * 24 * 60 * 60 * 1000  # 3日間
MS_PER_YEAR = 365 * 24 * 60 * 60 * 1000


def apr_sort_key(row: dict):
    """プラスのAPR (実行可能な現物買い+perpショート) を優先し、
    同符号内では絶対値が大きい順に並べる"""
    apr = row["funding_3d_apr_pct"]
    return (0, -apr) if apr > 0 else (1, apr)


def fetch_funding_history(contract_symbol: str, limit: int) -> list[dict]:
    req = urllib.request.Request(
        f"{API_URL}/api/v1/fundingRates?symbol={contract_symbol}&limit={limit}",
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_ticker_by_symbol() -> dict[str, dict]:
    data = fetch("/api/v1/tickers")
    return {d["symbol"]: d for d in data}


def get_mark_price_by_symbol() -> dict[str, dict]:
    data = fetch("/api/v1/markPrices")
    return {d["symbol"]: d for d in data}


def build_arbitrage_table(
    notional_usd: float, min_liquidity_usd: float
) -> tuple[list[dict], list[dict]]:
    matched = find_dual_listed()
    ticker_by_symbol = get_ticker_by_symbol()
    mark_by_symbol = get_mark_price_by_symbol()

    rows = []
    excluded = []

    for m in matched:
        perp_symbol = m["perp_symbol"]
        contract_symbol = m["perp_contract_symbol"]
        spot_ticker = ticker_by_symbol.get(m["spot_pair_name"])
        perp_mark = mark_by_symbol.get(contract_symbol)
        if spot_ticker is None or perp_mark is None:
            continue

        spot_volume_usd = float(spot_ticker["quoteVolume"])
        if spot_volume_usd < min_liquidity_usd:
            excluded.append({**m, "exchange": "Backpack", "spot_volume_usd": spot_volume_usd})
            continue

        interval_hours = m["funding_interval_ms"] / (60 * 60 * 1000)
        periods_per_year = MS_PER_YEAR / m["funding_interval_ms"]
        limit = max(1, min(1000, round(FUNDING_HISTORY_LOOKBACK_MS / m["funding_interval_ms"])))

        try:
            history = fetch_funding_history(contract_symbol, limit)
        except Exception:
            history = []
        rates = [float(h["fundingRate"]) for h in history]

        funding_now_period = float(perp_mark["fundingRate"])
        funding_now_apr_pct = funding_now_period * periods_per_year * 100

        if rates:
            funding_3d_avg_period = sum(rates) / len(rates)
            funding_3d_cum_pct = sum(rates) * 100
        else:
            funding_3d_avg_period = funding_now_period
            funding_3d_cum_pct = None
        funding_3d_apr_pct = funding_3d_avg_period * periods_per_year * 100

        spot_price = float(spot_ticker["lastPrice"])
        perp_price = float(perp_mark["markPrice"])
        basis_pct = (perp_price / spot_price - 1) * 100 if spot_price else None

        if funding_3d_avg_period > 0:
            spot_action = "買い(ロング)"
            perp_action = "ショート(売り)"
            note = ""
        elif funding_3d_avg_period < 0:
            spot_action = "売り"
            perp_action = "ロング(買い)"
            note = "現物の空売りは既に現物を保有している場合のみ実行可(Backpackのspotはフルコラテラルで新規空売り不可)"
        else:
            spot_action = "-"
            perp_action = "-"
            note = "funding がほぼ0のため裁定機会なし"

        if m["spot_quote_symbol"] != m["perp_quote_symbol"]:
            note = (
                note + " / " if note else ""
            ) + f"spotとperpのquote通貨が異なる({m['spot_quote_symbol']} vs {m['perp_quote_symbol']})ため価格比較に注意"

        est_3d_profit_usd = (
            notional_usd * funding_3d_cum_pct / 100 if funding_3d_cum_pct is not None else None
        )
        est_annual_profit_usd = notional_usd * funding_3d_apr_pct / 100

        rows.append(
            {
                "exchange": "Backpack",
                "perp_symbol": perp_symbol,
                "perp_contract_symbol": contract_symbol,
                "spot_pair_name": m["spot_pair_name"],
                "spot_quote_symbol": m["spot_quote_symbol"],
                "match_type": m["match_type"],
                "spot_action": spot_action,
                "perp_action": perp_action,
                "spot_price": spot_price,
                "perp_price": perp_price,
                "spot_volume_24h_usd": round(spot_volume_usd, 2),
                "basis_pct": round(basis_pct, 4) if basis_pct is not None else None,
                "funding_interval_hours": round(interval_hours, 2),
                "funding_now_period_pct": round(funding_now_period * 100, 6),
                "funding_now_apr_pct": round(funding_now_apr_pct, 4),
                "funding_3d_cum_pct": round(funding_3d_cum_pct, 4)
                if funding_3d_cum_pct is not None
                else None,
                "funding_3d_apr_pct": round(funding_3d_apr_pct, 4),
                "notional_usd": notional_usd,
                "est_3d_profit_usd": round(est_3d_profit_usd, 2)
                if est_3d_profit_usd is not None
                else None,
                "est_annual_profit_usd": round(est_annual_profit_usd, 2),
                "note": note,
            }
        )

    rows.sort(key=apr_sort_key)
    return rows, excluded


def write_json(rows: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def write_csv(rows: list[dict], path: str) -> None:
    fieldnames = [
        "exchange",
        "perp_symbol",
        "perp_contract_symbol",
        "spot_pair_name",
        "spot_quote_symbol",
        "match_type",
        "spot_action",
        "perp_action",
        "spot_price",
        "perp_price",
        "spot_volume_24h_usd",
        "basis_pct",
        "funding_interval_hours",
        "funding_now_period_pct",
        "funding_now_apr_pct",
        "funding_3d_cum_pct",
        "funding_3d_apr_pct",
        "notional_usd",
        "est_3d_profit_usd",
        "est_annual_profit_usd",
        "note",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Backpack の spot/perps 両建てファンディングレート・アービトラージ候補を一覧化する"
    )
    parser.add_argument("-f", "--format", choices=["csv", "json"], default="csv")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("-n", "--notional", type=float, default=10000.0)
    parser.add_argument("--min-liquidity-usd", type=float, default=20000.0)
    args = parser.parse_args()

    output_path = args.output or f"backpack_funding_arbitrage.{args.format}"
    rows, excluded = build_arbitrage_table(args.notional, args.min_liquidity_usd)

    if args.format == "json":
        write_json(rows, output_path)
    else:
        write_csv(rows, output_path)

    print(f"{len(rows)} 件の裁定候補を出力しました -> {output_path}", file=sys.stderr)
    if rows:
        top = rows[0]
        print(
            f"最大 |APR| (3日実績ベース): {top['perp_symbol']} "
            f"{top['funding_3d_apr_pct']}% ({top['spot_action']} / {top['perp_action']})",
            file=sys.stderr,
        )
    if excluded:
        names = ", ".join(
            f"{e['perp_symbol']}(24h出来高 ${e['spot_volume_usd']:,.0f})" for e in excluded
        )
        print(f"注: spot の流動性不足のため除外: {names}", file=sys.stderr)


if __name__ == "__main__":
    main()
