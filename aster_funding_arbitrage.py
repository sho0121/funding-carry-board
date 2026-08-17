#!/usr/bin/env python3
"""
Aster (asterdex.com) で spot / perps 両方に上場している銘柄について、
ファンディングレート・アービトラージ候補を一覧化する。
出力スキーマは hyperliquid_funding_arbitrage.py と共通で、
"exchange" フィールドでどちらの取引所かを区別する。

注意点:
  - Aster の funding 間隔は銘柄ごとに異なる (BTC/ETH/主要銘柄は8時間、
    新規/小型銘柄は4時間など)。固定値を仮定せず、fundingRate 履歴の
    タイムスタンプ間隔から実際の間隔を動的に算出して年率換算する。
  - spot 側の 24h 出来高 (quoteVolume) が --min-liquidity-usd 未満の
    銘柄は候補から除外する (Hyperliquid 版と同じ考え方)。
  - 同一ベースシンボルに対して perp 契約が複数存在する場合 (BTC/ETH/SOL は
    USDT/USD1/U 建てが並立) は USDT 建てを優先して採用する
    (aster_dual_listed.py 側で解決済み)。
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from aster_dual_listed import FAPI_URL, SAPI_URL, fetch, find_dual_listed  # noqa: E402

FUNDING_HISTORY_LOOKBACK_MS = 3 * 24 * 60 * 60 * 1000  # 3日間
MS_PER_YEAR = 365 * 24 * 60 * 60 * 1000


def apr_sort_key(row: dict):
    """プラスのAPR (実行可能な現物買い+perpショート) を優先し、
    同符号内では絶対値が大きい順に並べる"""
    apr = row["funding_3d_apr_pct"]
    return (0, -apr) if apr > 0 else (1, apr)


def fetch_funding_history(contract_symbol: str, start_time_ms: int) -> list[dict]:
    req = urllib.request.Request(
        f"{FAPI_URL}/fapi/v1/fundingRate?symbol={contract_symbol}&startTime={start_time_ms}",
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_perp_ctx_by_symbol() -> dict[str, dict]:
    data = fetch(FAPI_URL, "/fapi/v1/premiumIndex")
    return {d["symbol"]: d for d in data}


def get_spot_ctx_by_symbol() -> dict[str, dict]:
    data = fetch(SAPI_URL, "/api/v1/ticker/24hr")
    return {d["symbol"]: d for d in data}


def infer_interval_hours(history: list[dict]) -> float | None:
    times = sorted(h["fundingTime"] for h in history)
    if len(times) < 2:
        return None
    diffs_ms = [b - a for a, b in zip(times, times[1:])]
    return statistics.median(diffs_ms) / (60 * 60 * 1000)


def build_arbitrage_table(
    notional_usd: float, min_liquidity_usd: float
) -> tuple[list[dict], list[dict]]:
    matched = find_dual_listed()
    perp_ctx_by_symbol = get_perp_ctx_by_symbol()
    spot_ctx_by_symbol = get_spot_ctx_by_symbol()

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - FUNDING_HISTORY_LOOKBACK_MS

    rows = []
    excluded = []

    for m in matched:
        perp_symbol = m["perp_symbol"]
        contract_symbol = m["perp_contract_symbol"]
        perp_ctx = perp_ctx_by_symbol.get(contract_symbol)
        spot_ctx = spot_ctx_by_symbol.get(m["spot_pair_name"])
        if perp_ctx is None or spot_ctx is None:
            continue

        spot_volume_usd = float(spot_ctx["quoteVolume"])
        if spot_volume_usd < min_liquidity_usd:
            excluded.append({**m, "exchange": "Aster", "spot_volume_usd": spot_volume_usd})
            continue

        try:
            history = fetch_funding_history(contract_symbol, start_ms)
        except Exception:
            history = []
        rates = [float(h["fundingRate"]) for h in history]
        interval_hours = infer_interval_hours(history) or 8.0
        periods_per_year = MS_PER_YEAR / (interval_hours * 60 * 60 * 1000)

        funding_now_period = float(perp_ctx["lastFundingRate"])
        funding_now_apr_pct = funding_now_period * periods_per_year * 100

        if rates:
            funding_3d_avg_period = sum(rates) / len(rates)
            funding_3d_cum_pct = sum(rates) * 100
        else:
            funding_3d_avg_period = funding_now_period
            funding_3d_cum_pct = None
        funding_3d_apr_pct = funding_3d_avg_period * periods_per_year * 100

        spot_price = float(spot_ctx["lastPrice"])
        perp_price = float(perp_ctx["markPrice"])
        basis_pct = (perp_price / spot_price - 1) * 100 if spot_price else None

        if funding_3d_avg_period > 0:
            spot_action = "買い(ロング)"
            perp_action = "ショート(売り)"
            note = ""
        elif funding_3d_avg_period < 0:
            spot_action = "売り"
            perp_action = "ロング(買い)"
            note = "現物の空売りは既に現物を保有している場合のみ実行可(Asterのspotはフルコラテラルで新規空売り不可)"
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
                "exchange": "Aster",
                "perp_symbol": perp_symbol,
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
        description="Aster の spot/perps 両建てファンディングレート・アービトラージ候補を一覧化する"
    )
    parser.add_argument("-f", "--format", choices=["csv", "json"], default="csv")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("-n", "--notional", type=float, default=10000.0)
    parser.add_argument("--min-liquidity-usd", type=float, default=20000.0)
    args = parser.parse_args()

    output_path = args.output or f"aster_funding_arbitrage.{args.format}"
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
