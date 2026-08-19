#!/usr/bin/env python3
"""
Hyperliquid で spot / perps 両方に上場している銘柄について、
ファンディングレート・アービトラージ (現物と無期限先物を逆方向に建てて
価格変動リスクを抑えつつファンディングレートを受け取る戦略) の候補を一覧化する。
(参考: https://www.coinglass.com/ja/ArbitrageList )

戦略の考え方 (Hyperliquid の funding は「正の値 = ロングがショートに支払う」建て):
  - funding が プラス -> 現物を「買い」保有 + perps を「ショート」
                          => ショート側がファンディングを受け取れる
  - funding が マイナス -> 現物を「売り」 + perps を「ロング」
                          => ロング側がファンディングを受け取れる
    ただし Hyperliquid の spot は基本フルコラテラル (借り入れなし) のため、
    保有していない現物の「空売り」は通常できない。マイナスファンディングの
    銘柄はこの制約付きの注記を出力する。

銘柄の対応関係は hyperliquid_dual_listed.py の判定ロジックをそのまま再利用する。

--- 実データ検証で判明した注意点 (このスクリプトが自動で補正/除外する内容) ---

1. 出来高ゼロの死んだ spot ペアが存在する
   Hyperliquid の spot はパーミッションレスに誰でもペアを作成できるため、
   同じベースシンボルに対して 24h 出来高が $0 の空ペアが複数ぶら下がっている
   ことがある (例: BTC は @142 (出来高$1000万超) と @234 (出来高$0, quote が
   USDH) の 2 ペアが存在)。出来高ゼロのペアは実際には約定できず markPx も
   信頼できないため、同一 perp_symbol に複数 spot ペアがある場合は
   24h 出来高 (dayNtlVlm) が最大のものだけを採用する。
   さらに、選ばれたペアの出来高が --min-liquidity-usd 未満なら候補から除外する。

2. "k" プレフィックス perp (例: kBONK) は 1 コントラクト=対象トークン1000枚分の
   価格建てになっているため、素の spot 価格と単純比較すると基準がずれる。
   perp 価格を 1000 で割ってから spot 価格と比較する。

3. quote 通貨が USDC でない spot ペア (例: USDH 建て) は、quote 通貨自体が
   USD と 1:1 でない可能性があり、perps (USDC 建て) との価格比較の前提が
   崩れる。1 と同じ出来高ベースの選定で自然に排除されるが、念のため
   採用したペアの quote 通貨も出力し、USDC 以外なら注記を付ける。
"""

import argparse
import csv
import json
import sys
import time
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from hyperliquid_dual_listed import API_URL, fetch, find_dual_listed  # noqa: E402

HOURS_PER_YEAR = 24 * 365


def apr_sort_key(row: dict):
    """プラスのAPR (実行可能な現物買い+perpショート) を優先し、
    同符号内では絶対値が大きい順に並べる"""
    apr = row["funding_3d_apr_pct"]
    return (0, -apr) if apr > 0 else (1, apr)
FUNDING_HISTORY_LOOKBACK_MS = 3 * 24 * 60 * 60 * 1000  # 3日間
K_PREFIX_MULTIPLIER = 1000  # Hyperliquid の "k" プレフィックス perp の建て倍率


def fetch_funding_history(coin: str, start_time_ms: int) -> list[dict]:
    body = json.dumps(
        {"type": "fundingHistory", "coin": coin, "startTime": start_time_ms}
    ).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_perp_ctx_by_symbol() -> dict[str, dict]:
    meta, ctxs = fetch("metaAndAssetCtxs")
    return {entry["name"]: ctx for entry, ctx in zip(meta["universe"], ctxs)}


def get_spot_ctx_by_index() -> dict[int, dict]:
    meta, ctxs = fetch("spotMetaAndAssetCtxs")
    return {pair["index"]: ctxs[pair["index"]] for pair in meta["universe"]}


def pick_most_liquid_pair(matched: list[dict], spot_ctx_by_index: dict[int, dict]) -> list[dict]:
    """同一 perp_symbol に複数 spot ペアがある場合、24h 出来高最大のものだけ残す"""
    best_by_symbol: dict[str, tuple[float, dict]] = {}
    for m in matched:
        ctx = spot_ctx_by_index.get(m["spot_pair_index"])
        vol = float(ctx["dayNtlVlm"]) if ctx else 0.0
        current = best_by_symbol.get(m["perp_symbol"])
        if current is None or vol > current[0]:
            best_by_symbol[m["perp_symbol"]] = (vol, m)
    return [v[1] for v in best_by_symbol.values()]


def build_arbitrage_table(
    notional_usd: float, min_liquidity_usd: float
) -> tuple[list[dict], list[dict]]:
    matched, _ = find_dual_listed()
    perp_ctx_by_symbol = get_perp_ctx_by_symbol()
    spot_ctx_by_index = get_spot_ctx_by_index()

    deduped = pick_most_liquid_pair(matched, spot_ctx_by_index)

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - FUNDING_HISTORY_LOOKBACK_MS

    rows = []
    excluded = []

    for m in deduped:
        perp_symbol = m["perp_symbol"]
        perp_ctx = perp_ctx_by_symbol.get(perp_symbol)
        spot_ctx = spot_ctx_by_index.get(m["spot_pair_index"])
        if perp_ctx is None or spot_ctx is None:
            continue

        spot_volume_usd = float(spot_ctx["dayNtlVlm"])
        if spot_volume_usd < min_liquidity_usd:
            excluded.append({**m, "exchange": "Hyperliquid", "spot_volume_usd": spot_volume_usd})
            continue

        try:
            history = fetch_funding_history(perp_symbol, start_ms)
        except Exception:
            history = []
        rates = [float(h["fundingRate"]) for h in history]

        funding_now_hourly = float(perp_ctx["funding"])
        funding_now_apr_pct = funding_now_hourly * HOURS_PER_YEAR * 100

        if rates:
            funding_3d_avg_hourly = sum(rates) / len(rates)
            funding_3d_cum_pct = sum(rates) * 100
        else:
            funding_3d_avg_hourly = funding_now_hourly
            funding_3d_cum_pct = None
        funding_3d_apr_pct = funding_3d_avg_hourly * HOURS_PER_YEAR * 100

        spot_price = float(spot_ctx["markPx"])
        perp_price_raw = float(perp_ctx["markPx"])
        # "k" プレフィックス perp は 1000 トークン単位の建値なので正規化してから比較する
        perp_price_normalized = (
            perp_price_raw / K_PREFIX_MULTIPLIER
            if m["match_type"] == "unit_prefix_k"
            else perp_price_raw
        )
        basis_pct = (
            (perp_price_normalized / spot_price - 1) * 100 if spot_price else None
        )

        if funding_3d_avg_hourly > 0:
            spot_action = "買い(ロング)"
            perp_action = "ショート(売り)"
            note = ""
        elif funding_3d_avg_hourly < 0:
            spot_action = "売り"
            perp_action = "ロング(買い)"
            note = "現物の空売りは既に現物を保有している場合のみ実行可(Hyperliquid spotは原則フルコラテラルで新規空売り不可)"
        else:
            spot_action = "-"
            perp_action = "-"
            note = "funding がほぼ0のため裁定機会なし"

        if m["spot_quote_symbol"] != "USDC":
            note = (note + " / " if note else "") + f"quote通貨が{m['spot_quote_symbol']}(USDC以外)のため価格比較に注意"

        est_3d_profit_usd = (
            notional_usd * funding_3d_cum_pct / 100 if funding_3d_cum_pct is not None else None
        )
        est_annual_profit_usd = notional_usd * funding_3d_apr_pct / 100

        rows.append(
            {
                "exchange": "Hyperliquid",
                "perp_symbol": perp_symbol,
                "perp_contract_symbol": perp_symbol,
                "spot_pair_name": m["spot_pair_name"],
                "spot_quote_symbol": m["spot_quote_symbol"],
                "match_type": m["match_type"],
                "spot_action": spot_action,
                "perp_action": perp_action,
                "spot_price": spot_price,
                "perp_price": perp_price_raw,
                "spot_volume_24h_usd": round(spot_volume_usd, 2),
                "basis_pct": round(basis_pct, 4) if basis_pct is not None else None,
                "funding_interval_hours": 1,
                "funding_now_period_pct": round(funding_now_hourly * 100, 6),
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
        description=(
            "Hyperliquid の spot/perps 両建てファンディングレート・アービトラージ候補を一覧化する"
        )
    )
    parser.add_argument(
        "-f", "--format", choices=["csv", "json"], default="csv", help="出力フォーマット"
    )
    parser.add_argument(
        "-o", "--output", default=None, help="出力先ファイルパス (省略時は funding_arbitrage.<format>)"
    )
    parser.add_argument(
        "-n",
        "--notional",
        type=float,
        default=10000.0,
        help="想定ポジションサイズ (USD, デフォルト 10000) - 想定損益の試算に使用",
    )
    parser.add_argument(
        "--min-liquidity-usd",
        type=float,
        default=20000.0,
        help="この 24h spot 出来高(USD)未満の銘柄は候補から除外する (デフォルト 20000)",
    )
    args = parser.parse_args()

    output_path = args.output or f"funding_arbitrage.{args.format}"

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
        print(
            f"注: spot の流動性不足 (--min-liquidity-usd={args.min_liquidity_usd:,.0f} 未満) "
            f"のため除外: {names}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
