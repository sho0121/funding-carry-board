#!/usr/bin/env python3
"""
複数取引所 (Hyperliquid, Aster, ...) の spot/perps ファンディングレート・
アービトラージ候補を1つに統合する。

各取引所モジュールは共通スキーマの build_arbitrage_table(notional_usd, min_liquidity_usd)
-> (rows, excluded) を実装しており、本スクリプトはそれらを呼び出して結合するだけ。
新しい取引所を追加する場合は、同じスキーマの `<exchange>_funding_arbitrage.py` を
追加し、EXCHANGES に登録する。
"""

import argparse
import csv
import json
import sys

import aster_funding_arbitrage
import backpack_funding_arbitrage
import hyperliquid_funding_arbitrage
import injective_funding_arbitrage

EXCHANGES = {
    "hyperliquid": hyperliquid_funding_arbitrage,
    "aster": aster_funding_arbitrage,
    "backpack": backpack_funding_arbitrage,
    "injective": injective_funding_arbitrage,
}

FIELDNAMES = [
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


def build_combined_table(
    notional_usd: float, min_liquidity_usd: float, exchanges: list[str]
) -> tuple[list[dict], list[dict], dict[str, str]]:
    all_rows = []
    all_excluded = []
    errors = {}

    for name in exchanges:
        module = EXCHANGES[name]
        try:
            rows, excluded = module.build_arbitrage_table(notional_usd, min_liquidity_usd)
        except Exception as e:  # 1取引所のAPI障害で全体を止めない
            errors[name] = str(e)
            continue
        all_rows.extend(rows)
        all_excluded.extend(excluded)

    all_rows.sort(key=hyperliquid_funding_arbitrage.apr_sort_key)
    return all_rows, all_excluded, errors


def write_json(rows: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="複数取引所の spot/perps ファンディングレート・アービトラージ候補を統合する"
    )
    parser.add_argument("-f", "--format", choices=["csv", "json"], default="csv")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("-n", "--notional", type=float, default=10000.0)
    parser.add_argument("--min-liquidity-usd", type=float, default=20000.0)
    parser.add_argument(
        "--exchanges",
        default=",".join(EXCHANGES.keys()),
        help=f"対象取引所をカンマ区切りで指定 (デフォルト: 全て = {','.join(EXCHANGES.keys())})",
    )
    args = parser.parse_args()

    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()]
    unknown = [e for e in exchanges if e not in EXCHANGES]
    if unknown:
        print(f"未知の取引所: {unknown} (選択可: {list(EXCHANGES.keys())})", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or f"multi_exchange_arbitrage.{args.format}"
    rows, excluded, errors = build_combined_table(args.notional, args.min_liquidity_usd, exchanges)

    if args.format == "json":
        write_json(rows, output_path)
    else:
        write_csv(rows, output_path)

    by_exchange = {}
    for r in rows:
        by_exchange[r["exchange"]] = by_exchange.get(r["exchange"], 0) + 1
    print(f"{len(rows)} 件の裁定候補を出力しました -> {output_path} ({by_exchange})", file=sys.stderr)
    if rows:
        top = rows[0]
        print(
            f"最大 |APR|: [{top['exchange']}] {top['perp_symbol']} "
            f"{top['funding_3d_apr_pct']}% ({top['spot_action']} / {top['perp_action']})",
            file=sys.stderr,
        )
    if errors:
        print(f"取得に失敗した取引所: {errors}", file=sys.stderr)


if __name__ == "__main__":
    main()
