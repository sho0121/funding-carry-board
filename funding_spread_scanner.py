#!/usr/bin/env python3
"""
Perp 対 Perp のファンディングレート差分アービトラージ候補をスキャンする。

spot を一切使わず、無期限先物同士を逆方向に建てて (両脚とも証拠金取引 =
レバレッジ可) ファンディングレートの「差分」を取りに行く戦略の候補を探す。

2種類の機会を検出する:

  1. cross_exchange: 同じ銘柄の perp が複数取引所に上場している場合、
     funding が一番高い取引所でショート・一番低い(またはマイナスに近い)
     取引所でロングし、差分を狙う。取引所間の送金・出金リスクがある。

  2. same_exchange: 同一取引所内で同じ原資産に対して決済通貨違いの複数
     perp 契約がある場合 (例: Aster の BTCUSDT / BTCUSD1 / BTCU)、
     その中でショート・ロングを組む。送金リスクは無いが、決済通貨自体の
     デペッグリスクがある。

対象取引所: Hyperliquid, Aster, Backpack (出来高フィルタあり)。
Injective は現状 24h 出来高が取得できないため「参考情報」として別枠で扱う。

funding rate は本スクリプトでは「現在の瞬間レート」を使う (3日実績平均では
ない)。取引所間の一括比較を軽量に行うため、全銘柄について履歴を遡る代わりに
各取引所の bulk エンドポイントで取れる現在値をそのまま使っている。
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.request

HL_API = "https://api.hyperliquid.xyz/info"
ASTER_FAPI = "https://fapi.asterdex.com"
BACKPACK_API = "https://api.backpack.exchange"
INJECTIVE_API = "https://sentry.exchange.grpc-web.injective.network"

MS_PER_YEAR = 365 * 24 * 60 * 60 * 1000
FUNDING_HISTORY_LOOKBACK_MS = 3 * 24 * 60 * 60 * 1000  # 3日間
TOP_N_TO_VERIFY = 30  # 瞬間値スクリーニング後、3日実績で検証する候補数


def _get_json(url: str, method: str = "GET", body: dict | None = None) -> object:
    headers = {"User-Agent": "Mozilla/5.0"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 取引所ごとの perp 一覧 (spot 上場は問わない) を共通スキーマで返す
#   {exchange, base_symbol, contract_symbol, quote_symbol, funding_apr_pct,
#    mark_price, volume_24h_usd (None なら不明)}
# ---------------------------------------------------------------------------


def fetch_hyperliquid_perps() -> list[dict]:
    meta, ctxs = _get_json(HL_API, "POST", {"type": "metaAndAssetCtxs"})
    rows = []
    for entry, ctx in zip(meta["universe"], ctxs):
        if entry.get("isDelisted"):
            continue
        funding_hourly = float(ctx["funding"])
        rows.append(
            {
                "exchange": "Hyperliquid",
                "base_symbol": entry["name"],
                "contract_symbol": entry["name"],
                "quote_symbol": "USDC",
                "funding_apr_pct": funding_hourly * (365 * 24) * 100,
                "mark_price": float(ctx["markPx"]),
                "volume_24h_usd": float(ctx["dayNtlVlm"]),
            }
        )
    return rows


def fetch_aster_perps() -> list[dict]:
    exchange_info = _get_json(f"{ASTER_FAPI}/fapi/v1/exchangeInfo")
    base_by_symbol = {
        s["symbol"]: s["baseAsset"]
        for s in exchange_info["symbols"]
        if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
    }
    premium = _get_json(f"{ASTER_FAPI}/fapi/v1/premiumIndex")
    funding_info = _get_json(f"{ASTER_FAPI}/fapi/v1/fundingInfo")
    interval_by_symbol = {f["symbol"]: f["fundingIntervalHours"] for f in funding_info}
    ticker = _get_json(f"{ASTER_FAPI}/fapi/v1/ticker/24hr")
    volume_by_symbol = {t["symbol"]: float(t["quoteVolume"]) for t in ticker}

    rows = []
    for p in premium:
        symbol = p["symbol"]
        base = base_by_symbol.get(symbol)
        if base is None:
            continue
        interval_hours = interval_by_symbol.get(symbol, 8)
        periods_per_year = (365 * 24) / interval_hours
        funding_period = float(p["lastFundingRate"])
        rows.append(
            {
                "exchange": "Aster",
                "base_symbol": base,
                "contract_symbol": symbol,
                "quote_symbol": symbol[len(base):],
                "funding_apr_pct": funding_period * periods_per_year * 100,
                "mark_price": float(p["markPrice"]),
                "volume_24h_usd": volume_by_symbol.get(symbol),
            }
        )
    return rows


def fetch_backpack_perps() -> list[dict]:
    markets = _get_json(f"{BACKPACK_API}/api/v1/markets")
    perp_markets = {
        m["symbol"]: m for m in markets if m["marketType"] == "PERP" and m.get("visible", True)
    }
    mark_prices = _get_json(f"{BACKPACK_API}/api/v1/markPrices")
    mark_by_symbol = {m["symbol"]: m for m in mark_prices}
    tickers = _get_json(f"{BACKPACK_API}/api/v1/tickers")
    volume_by_symbol = {t["symbol"]: float(t["quoteVolume"]) for t in tickers}

    rows = []
    for symbol, market in perp_markets.items():
        mark = mark_by_symbol.get(symbol)
        if mark is None:
            continue
        interval_hours = market["fundingInterval"] / (60 * 60 * 1000)
        periods_per_year = (365 * 24) / interval_hours
        funding_period = float(mark["fundingRate"])
        rows.append(
            {
                "exchange": "Backpack",
                "base_symbol": market["baseSymbol"],
                "contract_symbol": symbol,
                "quote_symbol": market["quoteSymbol"],
                "funding_apr_pct": funding_period * periods_per_year * 100,
                "mark_price": float(mark["markPrice"]),
                "volume_24h_usd": volume_by_symbol.get(symbol),
            }
        )
    return rows


# Injectiveのfunding履歴API (fundingRates?marketId=...) はtickerではなくmarketIdの
# hashを要求するため、表示用ticker -> marketId の対応をここに保持しておく
# (fetch_injective_perps() 実行時に埋まる。fetch_3d_avg_apr() から参照する)
_INJECTIVE_MARKET_ID_BY_TICKER: dict[str, str] = {}


def fetch_injective_perps() -> list[dict]:
    """出来高が取得できないため volume_24h_usd は常に None (参考情報扱い)"""
    data = _get_json(f"{INJECTIVE_API}/api/exchange/derivative/v1/markets")
    rows = []
    for m in data["markets"]:
        if m["marketStatus"] != "active" or not m.get("isPerpetual"):
            continue
        funding_info = m.get("perpetualMarketInfo") or {}
        funding_state = m.get("perpetualMarketFunding") or {}
        interval_seconds = funding_info.get("fundingInterval")
        last_rate = funding_state.get("lastFundingRate")
        if not interval_seconds or last_rate is None:
            continue
        periods_per_year = (365 * 24 * 3600) / interval_seconds
        base = m["ticker"].split("/")[0]
        _INJECTIVE_MARKET_ID_BY_TICKER[m["ticker"]] = m["marketId"]
        rows.append(
            {
                "exchange": "Injective",
                "base_symbol": base,
                "contract_symbol": m["ticker"],
                "quote_symbol": m["ticker"].split("/")[1].replace(" PERP", ""),
                "funding_apr_pct": float(last_rate) * periods_per_year * 100,
                "mark_price": None,
                "volume_24h_usd": None,
            }
        )
    return rows


FETCHERS = {
    "Hyperliquid": fetch_hyperliquid_perps,
    "Aster": fetch_aster_perps,
    "Backpack": fetch_backpack_perps,
    "Injective": fetch_injective_perps,
}


# ---------------------------------------------------------------------------
# 上位候補のみ、3日間の実績平均 funding で検証する (瞬間値だけだと薄い銘柄の
# 年率換算が非現実的に跳ねるため。過去に main dashboard で学んだのと同じ理由)
# ---------------------------------------------------------------------------


def fetch_3d_avg_apr(exchange: str, contract_symbol: str, base_symbol: str) -> float | None:
    start_ms = int(time.time() * 1000) - FUNDING_HISTORY_LOOKBACK_MS
    try:
        if exchange == "Hyperliquid":
            history = _get_json(
                HL_API, "POST", {"type": "fundingHistory", "coin": base_symbol, "startTime": start_ms}
            )
            rates = [float(h["fundingRate"]) for h in history]
            if not rates:
                return None
            return (sum(rates) / len(rates)) * (365 * 24) * 100

        if exchange == "Aster":
            history = _get_json(
                f"{ASTER_FAPI}/fapi/v1/fundingRate?symbol={contract_symbol}&startTime={start_ms}"
            )
            if not history:
                return None
            times = sorted(h["fundingTime"] for h in history)
            diffs = [b - a for a, b in zip(times, times[1:])]
            interval_hours = statistics.median(diffs) / (3600 * 1000) if diffs else 8
            rates = [float(h["fundingRate"]) for h in history]
            periods_per_year = (365 * 24) / interval_hours
            return (sum(rates) / len(rates)) * periods_per_year * 100

        if exchange == "Backpack":
            history = _get_json(
                f"{BACKPACK_API}/api/v1/fundingRates?symbol={contract_symbol}&limit=100"
            )
            if not history:
                return None
            # intervalEndTimestamp は ISO8601 文字列。3日以内のものだけ使う
            cutoff = time.time() - FUNDING_HISTORY_LOOKBACK_MS / 1000
            recent = [
                h for h in history
                if time.mktime(time.strptime(h["intervalEndTimestamp"], "%Y-%m-%dT%H:%M:%S")) >= cutoff
            ]
            recent = recent or history[:72]
            rates = [float(h["fundingRate"]) for h in recent]
            # Backpack の perp は現状全て1時間間隔
            return (sum(rates) / len(rates)) * (365 * 24) * 100

        if exchange == "Injective":
            market_id = _INJECTIVE_MARKET_ID_BY_TICKER.get(contract_symbol)
            if market_id is None:
                return None
            history = _get_json(
                f"{INJECTIVE_API}/api/exchange/derivative/v1/fundingRates?marketId={market_id}&limit=100"
            )
            rates_data = history.get("fundingRates") or []
            if not rates_data:
                return None
            recent = [h for h in rates_data if h["timestamp"] >= start_ms]
            recent = recent or rates_data[:72]
            times = sorted(h["timestamp"] for h in recent)
            diffs = [b - a for a, b in zip(times, times[1:])]
            interval_hours = statistics.median(diffs) / (3600 * 1000) if diffs else 1.0
            rates = [float(h["rate"]) for h in recent]
            periods_per_year = (365 * 24) / interval_hours
            return (sum(rates) / len(rates)) * periods_per_year * 100

        return None
    except Exception:
        return None


def build_spread_table(
    min_liquidity_usd: float, exchanges: list[str]
) -> list[dict]:
    all_perps: list[dict] = []
    for ex in exchanges:
        all_perps.extend(FETCHERS[ex]())

    by_base: dict[str, list[dict]] = {}
    for p in all_perps:
        by_base.setdefault(p["base_symbol"], []).append(p)

    rows = []
    for base, entries in by_base.items():
        if len(entries) < 2:
            continue

        # 出来高フィルタ (Injective など volume_24h_usd が None の行は
        # 「参考」として残すが、フィルタの判定には使わない)
        tradable = [
            e for e in entries if e["volume_24h_usd"] is None or e["volume_24h_usd"] >= min_liquidity_usd
        ]
        if len(tradable) < 2:
            continue

        # 候補ペアを列挙する: (1) 全体最適 (取引所を問わず best short + best long)、
        # (2) 同一取引所内に複数契約がある場合はその中での best short + best long。
        # (2) は (1) が cross_exchange の場合に隠れてしまうため、別行として必ず出す。
        candidate_pairs = []

        overall_short = max(tradable, key=lambda e: e["funding_apr_pct"])
        overall_long = min(tradable, key=lambda e: e["funding_apr_pct"])
        if overall_short is not overall_long:
            candidate_pairs.append((overall_short, overall_long))

        by_exchange_local: dict[str, list[dict]] = {}
        for e in tradable:
            by_exchange_local.setdefault(e["exchange"], []).append(e)
        for ex_entries in by_exchange_local.values():
            if len(ex_entries) < 2:
                continue
            s = max(ex_entries, key=lambda e: e["funding_apr_pct"])
            l = min(ex_entries, key=lambda e: e["funding_apr_pct"])
            if s is not l:
                candidate_pairs.append((s, l))

        seen_pairs = set()
        for short_leg, long_leg in candidate_pairs:
            pair_key = (short_leg["contract_symbol"], short_leg["exchange"], long_leg["contract_symbol"], long_leg["exchange"])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            net_apr_now = short_leg["funding_apr_pct"] - long_leg["funding_apr_pct"]
            if net_apr_now <= 0:
                continue

            spread_type = "same_exchange" if short_leg["exchange"] == long_leg["exchange"] else "cross_exchange"

            price_diff_pct = None
            if short_leg["mark_price"] and long_leg["mark_price"]:
                price_diff_pct = (short_leg["mark_price"] / long_leg["mark_price"] - 1) * 100

            note = ""
            if spread_type == "cross_exchange":
                note = "取引所間の送金・出金リスクあり。両取引所に資金を分けて用意する必要がある"
            elif short_leg["quote_symbol"] != long_leg["quote_symbol"]:
                note = f"決済通貨が異なる({long_leg['quote_symbol']} vs {short_leg['quote_symbol']})。決済通貨自体のデペッグリスクに注意"
            if short_leg["volume_24h_usd"] is None or long_leg["volume_24h_usd"] is None:
                note = (note + " / " if note else "") + "出来高不明な脚を含む(参考情報)"

            rows.append(
                {
                    "base_symbol": base,
                    "spread_type": spread_type,
                    "short_exchange": short_leg["exchange"],
                    "short_contract": short_leg["contract_symbol"],
                    "short_funding_apr_now_pct": round(short_leg["funding_apr_pct"], 4),
                    "short_price": short_leg["mark_price"],
                    "short_volume_24h_usd": short_leg["volume_24h_usd"],
                    "long_exchange": long_leg["exchange"],
                    "long_contract": long_leg["contract_symbol"],
                    "long_funding_apr_now_pct": round(long_leg["funding_apr_pct"], 4),
                    "long_price": long_leg["mark_price"],
                    "long_volume_24h_usd": long_leg["volume_24h_usd"],
                    "net_apr_now_pct": round(net_apr_now, 4),
                    "price_diff_pct": round(price_diff_pct, 4) if price_diff_pct is not None else None,
                    "note": note,
                }
            )

    # まず瞬間値で足切りし、上位候補だけ3日実績平均で検証する。
    # 出来高不明(参考情報, 主にInjective絡み)の行が瞬間値の極端さで
    # 上位を占めがちなので、実際に取引可能な行 (両脚とも出来高判明) を
    # 優先して検証枠に回す。
    rows.sort(key=lambda r: r["net_apr_now_pct"], reverse=True)
    tradable_rows = [
        r for r in rows if r["short_volume_24h_usd"] is not None and r["long_volume_24h_usd"] is not None
    ]
    reference_rows = [r for r in rows if r["short_volume_24h_usd"] is None or r["long_volume_24h_usd"] is None]

    REFERENCE_VERIFY_CAP = 25  # Injectiveのfunding履歴取得に対応したため引き上げた
    to_verify = tradable_rows[:TOP_N_TO_VERIFY] + reference_rows[:REFERENCE_VERIFY_CAP]
    verify_ids = {id(r) for r in to_verify}
    rest = [r for r in rows if id(r) not in verify_ids]

    for r in to_verify:
        short_3d = fetch_3d_avg_apr(r["short_exchange"], r["short_contract"], r["base_symbol"])
        long_3d = fetch_3d_avg_apr(r["long_exchange"], r["long_contract"], r["base_symbol"])
        r["short_funding_apr_3d_pct"] = round(short_3d, 4) if short_3d is not None else None
        r["long_funding_apr_3d_pct"] = round(long_3d, 4) if long_3d is not None else None
        if short_3d is not None and long_3d is not None:
            r["net_apr_3d_pct"] = round(short_3d - long_3d, 4)
            if r["net_apr_3d_pct"] <= 0:
                r["note"] = (
                    (r["note"] + " / " if r["note"] else "")
                    + "3日実績では差分がマイナスに転じており、瞬間値ほど安定した機会ではない"
                )
        else:
            r["net_apr_3d_pct"] = None
            r["note"] = (r["note"] + " / " if r["note"] else "") + "3日実績データ取得不可のため瞬間値のみ"

    for r in rest:
        r["short_funding_apr_3d_pct"] = None
        r["long_funding_apr_3d_pct"] = None
        r["net_apr_3d_pct"] = None
        r["note"] = (r["note"] + " / " if r["note"] else "") + "上位候補外のため3日実績は未検証(瞬間値のみ)"

    all_rows = to_verify + rest

    def sort_key(r):
        # 3日実績で検証済みの行を常に未検証の行より優先する
        # (瞬間値だけの薄い銘柄が非現実的に大きい数値で上位に残るのを防ぐため)
        verified = r["net_apr_3d_pct"] is not None
        value = r["net_apr_3d_pct"] if verified else r["net_apr_now_pct"]
        return (1 if verified else 0, value)

    all_rows.sort(key=sort_key, reverse=True)
    return all_rows


def write_json(rows: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def write_csv(rows: list[dict], path: str) -> None:
    fieldnames = [
        "base_symbol",
        "spread_type",
        "short_exchange",
        "short_contract",
        "short_funding_apr_now_pct",
        "short_funding_apr_3d_pct",
        "short_price",
        "short_volume_24h_usd",
        "long_exchange",
        "long_contract",
        "long_funding_apr_now_pct",
        "long_funding_apr_3d_pct",
        "long_price",
        "long_volume_24h_usd",
        "net_apr_now_pct",
        "net_apr_3d_pct",
        "price_diff_pct",
        "note",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Perp対Perpのファンディングレート差分アービトラージ候補をスキャンする"
    )
    parser.add_argument("-f", "--format", choices=["csv", "json"], default="csv")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument("--min-liquidity-usd", type=float, default=20000.0)
    parser.add_argument(
        "--exchanges", default="Hyperliquid,Aster,Backpack,Injective"
    )
    args = parser.parse_args()

    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()]
    output_path = args.output or f"funding_spread.{args.format}"

    rows = build_spread_table(args.min_liquidity_usd, exchanges)

    if args.format == "json":
        write_json(rows, output_path)
    else:
        write_csv(rows, output_path)

    print(f"{len(rows)} 件のスプレッド候補を出力しました -> {output_path}", file=sys.stderr)
    if rows:
        top = rows[0]
        net = top["net_apr_3d_pct"] if top["net_apr_3d_pct"] is not None else top["net_apr_now_pct"]
        print(
            f"最大差分: {top['base_symbol']} ショート[{top['short_exchange']}] "
            f"vs ロング[{top['long_exchange']}] = {net}%",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
