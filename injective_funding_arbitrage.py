#!/usr/bin/env python3
"""
Injective (Helix) で spot / perps 両方に上場している銘柄について、
ファンディングレート・アービトラージ候補を一覧化する。
出力スキーマは他取引所のスクリプトと共通で、"exchange" フィールドで区別する。

--- 他取引所との違い (簡易実装であることの明記) ---

Injective の Indexer API は gRPC-Web ベースで、価格・24h出来高・funding履歴を
返す REST エンドポイント (旧 chronos サービス) の現在の正しいパスが
たどれなかった (候補ホストが軒並み 404/503 で、公式 Python SDK もこの環境では
インストールできなかった)。そのため、このモジュールは以下の点で他取引所版より
情報が少ない簡易実装になっている:

  - spot_price / perp_price / basis_pct は取得できないため常に None
    (perp market メタデータの perpetualMarketFunding には現在の funding rate は
    含まれるが、現在の取引価格そのものは含まれていない)
  - 24h 出来高が取得できないため、流動性フィルタ (--min-liquidity-usd) は
    適用されない。マッチした全銘柄がそのまま候補になる (流動性は自己責任で確認)
  - 3日間の実績平均が取得できないため、funding_3d_apr_pct は
    funding_now_apr_pct (現在の瞬間レート) をそのまま流用した近似値
    (funding_3d_cum_pct は None のまま)

将来的に正しい価格/出来高エンドポイント、または公式 SDK が利用可能になれば、
他取引所と同じ精度に引き上げられる。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

from injective_dual_listed import fetch, find_dual_listed

HOURS_PER_YEAR = 24 * 365


def apr_sort_key(row: dict):
    """プラスのAPR (実行可能な現物買い+perpショート) を優先し、
    同符号内では絶対値が大きい順に並べる"""
    apr = row["funding_3d_apr_pct"]
    return (0, -apr) if apr > 0 else (1, apr)
DATA_LIMITATION_NOTE = (
    "簡易実装: Injective の価格/出来高/funding履歴 REST エンドポイントが未特定のため、"
    "現在のfunding rateのみを参考情報として表示(価格・ベーシス・出来高フィルタなし)"
)


def build_arbitrage_table(
    notional_usd: float, min_liquidity_usd: float
) -> tuple[list[dict], list[dict]]:
    """min_liquidity_usd は他取引所とのインターフェース互換のために受け取るが、
    出来高データが取得できないため実際にはフィルタしない。"""
    matched = find_dual_listed()
    perp_ctx_by_market_id = {
        mkt["marketId"]: mkt for mkt in fetch("/api/exchange/derivative/v1/markets")["markets"]
    }

    rows = []
    for m in matched:
        interval_seconds = m["funding_interval_seconds"]
        if not interval_seconds:
            continue
        interval_hours = interval_seconds / 3600
        periods_per_year = (365 * 24 * 3600) / interval_seconds

        perp_ctx = perp_ctx_by_market_id.get(m["perp_market_id"])
        if perp_ctx is None:
            continue

        funding_state = perp_ctx.get("perpetualMarketFunding") or {}
        last_funding_rate = funding_state.get("lastFundingRate")
        if last_funding_rate is None:
            continue
        funding_now_period = float(last_funding_rate)
        funding_now_apr_pct = funding_now_period * periods_per_year * 100

        if funding_now_period > 0:
            spot_action = "買い(ロング)"
            perp_action = "ショート(売り)"
            note = DATA_LIMITATION_NOTE
        elif funding_now_period < 0:
            spot_action = "売り"
            perp_action = "ロング(買い)"
            note = (
                DATA_LIMITATION_NOTE
                + " / 現物の空売りは既に現物を保有している場合のみ実行可"
            )
        else:
            spot_action = "-"
            perp_action = "-"
            note = DATA_LIMITATION_NOTE + " / funding がほぼ0のため裁定機会なし"

        est_annual_profit_usd = notional_usd * funding_now_apr_pct / 100

        rows.append(
            {
                "exchange": "Injective",
                "perp_symbol": m["perp_symbol"],
                "spot_pair_name": m["spot_ticker"],
                "spot_quote_symbol": m["spot_ticker"].split("/")[1],
                "match_type": m["match_type"],
                "spot_action": spot_action,
                "perp_action": perp_action,
                "spot_price": None,
                "perp_price": None,
                "spot_volume_24h_usd": None,
                "basis_pct": None,
                "funding_interval_hours": round(interval_hours, 2),
                "funding_now_period_pct": round(funding_now_period * 100, 6),
                "funding_now_apr_pct": round(funding_now_apr_pct, 4),
                "funding_3d_cum_pct": None,
                "funding_3d_apr_pct": round(funding_now_apr_pct, 4),
                "notional_usd": notional_usd,
                "est_3d_profit_usd": None,
                "est_annual_profit_usd": round(est_annual_profit_usd, 2),
                "note": note,
            }
        )

    rows.sort(key=apr_sort_key)
    return rows, []


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
        description="Injective (Helix) の spot/perps ファンディングレート候補を一覧化する (簡易版)"
    )
    parser.add_argument("-f", "--format", choices=["csv", "json"], default="csv")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("-n", "--notional", type=float, default=10000.0)
    parser.add_argument("--min-liquidity-usd", type=float, default=20000.0)
    args = parser.parse_args()

    output_path = args.output or f"injective_funding_arbitrage.{args.format}"
    rows, excluded = build_arbitrage_table(args.notional, args.min_liquidity_usd)

    if args.format == "json":
        write_json(rows, output_path)
    else:
        write_csv(rows, output_path)

    print(f"{len(rows)} 件の裁定候補を出力しました -> {output_path}", file=sys.stderr)
    print("注: 簡易実装のため価格・出来高フィルタなし。詳細はスクリプト先頭のdocstring参照", file=sys.stderr)


if __name__ == "__main__":
    main()
