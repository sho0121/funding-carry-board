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

対象取引所: Hyperliquid, Aster, Backpack, Injective, edgeX, dYdX, ApeX。Injective は瞬間値
スクリーニング段階では出来高不明のまま候補に残すが、出来高不明な行は(実際に取引可能な
上位N件に加えて)**全件**、約定履歴 (/api/exchange/derivative/v1/trades) から実際の
24h出来高を遡って計算し補完する。補完の結果、実出来高が --min-liquidity-usd 未満と
判明した行は excluded に回す。一部だけ検証すると、検証対象外の下位候補に実在しない
ゴースト市場が紛れ込んだまま表示され続けてしまうため、正確性を優先して全件検証する
(GitHub Actionsに実行時間の制約は無い)。

Injectiveのチェーン(Exchange module)は誰でも無許可でデリバティブ市場を作成できるため、
このAPI (sentry.exchange.grpc-web.injective.network) はHelix(公式フロントエンド)が
実際に採用・キュレーションしていない市場も "active" として無差別に返す。実例: AR/USDC
PERPはAPI上activeで極少額の約定履歴もあったが、Helixの検索・市場一覧には一切出てこない
ことを実機検証で確認した(2026-08-20)。APIからは「Helix採用済みか」を示す明示的な
フラグが取れないため、出来高が閾値未満かどうかを代理指標として使っている
(無許可・非採用の市場は実質誰も使わないため出来高がほぼゼロになる傾向がある)。
ファンディングキャリー側の出来高フィルタ・excludedリストと同じ考え方。

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


# Injectiveのfunding履歴/約定履歴APIはtickerではなくmarketIdの hash を要求するため、
# 表示用ticker -> {market_id, quote_decimals} の対応をここに保持しておく
# (fetch_injective_perps() 実行時に埋まる。fetch_3d_avg_apr() / 出来高取得から参照する)
_INJECTIVE_MARKET_META_BY_TICKER: dict[str, dict] = {}


def fetch_injective_perps() -> list[dict]:
    """瞬間値取得の時点では出来高は不明 (None)。上位候補になった行のみ、検証フェーズで
    fetch_injective_24h_volume_usd() により実際の出来高を遡って補完する。"""
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
        _INJECTIVE_MARKET_META_BY_TICKER[m["ticker"]] = {
            "market_id": m["marketId"],
            "quote_decimals": m["quoteTokenMeta"]["decimals"],
        }
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


def fetch_injective_24h_volume_usd(contract_symbol: str) -> tuple[float | None, bool]:
    """直近24hの約定履歴からUSD建て出来高を概算する。
    価格は生の executionPrice を quote_decimals で割ればHyperliquidの実勢価格と一致する
    ことを実データで検証済み(quantityは既に人間可読な単位で返る)。
    戻り値の bool は「1000件上限に達し過小評価の可能性があるか」(False=不完全)。"""
    meta = _INJECTIVE_MARKET_META_BY_TICKER.get(contract_symbol)
    if meta is None:
        return None, False
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 24 * 60 * 60 * 1000
    try:
        data = _get_json(
            f"{INJECTIVE_API}/api/exchange/derivative/v1/trades"
            f"?marketId={meta['market_id']}&startTime={start_ms}&endTime={now_ms}&limit=1000"
        )
    except Exception:
        return None, False

    trades = data.get("trades") or []
    if not trades:
        return 0.0, True

    paging = data.get("paging") or {}
    complete = int(paging.get("total", len(trades))) <= len(trades)
    scale = 10 ** meta["quote_decimals"]
    volume = 0.0
    for t in trades:
        delta = t.get("positionDelta") or {}
        price, qty = delta.get("executionPrice"), delta.get("executionQuantity")
        if price is None or qty is None:
            continue
        volume += (float(price) / scale) * float(qty)
    return volume, complete


EDGEX_API = "https://edgex-prod-v2.edgex.exchange"


def fetch_edgex_perps() -> list[dict]:
    """契約一覧はメタデータAPIで一括取得できるが、funding/価格/出来高のbulk取得
    エンドポイントが見つからず契約ごとに呼ぶ必要がある(Injectiveと同様のパターン)。
    edgeXはperpのみでspot取引が無いため、キャリー(spot+perp)側には登録しない。"""
    meta = _get_json(f"{EDGEX_API}/api/v2/public/meta/getMetaData")
    contracts = meta["data"]["contractList"]

    rows = []
    for c in contracts:
        if not c.get("enableTrade"):
            continue
        contract_id = c["contractId"]
        name = c["contractName"]
        interval_min = float(c.get("fundingRateIntervalMin") or 240)

        try:
            ticker_list = _get_json(
                f"{EDGEX_API}/api/v2/public/quote/getTicker?contractId={contract_id}"
            ).get("data") or []
        except Exception:
            continue
        if not ticker_list:
            continue
        ticker = ticker_list[0]

        funding_rate = ticker.get("fundingRate")
        mark_price = ticker.get("markPrice") or ticker.get("lastPrice")
        if funding_rate is None or mark_price is None:
            continue
        volume = ticker.get("value")
        periods_per_year = (365 * 24 * 60) / interval_min
        base = name[:-4] if name.endswith("USDC") else name  # 全契約USDC建てのため末尾を除去

        rows.append(
            {
                "exchange": "edgeX",
                "base_symbol": base,
                "contract_symbol": name,
                "quote_symbol": "USDC",
                "funding_apr_pct": float(funding_rate) * periods_per_year * 100,
                "mark_price": float(mark_price),
                "volume_24h_usd": float(volume) if volume is not None else None,
            }
        )
    return rows


DYDX_API = "https://indexer.dydx.trade/v4"


def fetch_dydx_perps() -> list[dict]:
    """他のbulk系取引所(Hyperliquid/Aster/Backpack)と同様、1回のAPI呼び出しで
    全銘柄を取得できる。fundingは1時間ごとのfunding-tick epochで決まる
    (dYdX v4ドキュメントで確認済み)。dYdXはperpのみでspot取引が無いため、
    キャリー(spot+perp)側には登録しない。"""
    data = _get_json(f"{DYDX_API}/perpetualMarkets")
    rows = []
    for ticker, m in (data.get("markets") or {}).items():
        if m.get("status") != "ACTIVE":
            continue
        funding_rate = m.get("nextFundingRate")
        oracle_price = m.get("oraclePrice")
        if funding_rate is None or oracle_price is None:
            continue
        volume = m.get("volume24H")
        rows.append(
            {
                "exchange": "dYdX",
                "base_symbol": ticker.split("-")[0],
                "contract_symbol": ticker,
                "quote_symbol": "USD",
                "funding_apr_pct": float(funding_rate) * (365 * 24) * 100,
                "mark_price": float(oracle_price),
                "volume_24h_usd": float(volume) if volume is not None else None,
            }
        )
    return rows


APEX_API = "https://omni.apex.exchange/api/v3"


def fetch_apex_perps() -> list[dict]:
    """契約一覧はメタデータAPIで一括取得できるが、funding/価格/出来高のbulk取得
    エンドポイントが見つからず契約ごとに呼ぶ必要がある(edgeX/Injectiveと同様の
    パターン)。fundingは1時間ごとと確認済み。ApeXはperpのみでspot取引が無いため、
    キャリー(spot+perp)側には登録しない。"""
    meta = _get_json(f"{APEX_API}/symbols")
    contracts = meta["data"]["contractConfig"]["perpetualContract"]

    rows = []
    for c in contracts:
        if not c.get("enableTrade"):
            continue
        symbol = c["symbol"]  # 例: "BTC-USDT"
        ticker_symbol = symbol.replace("-", "")  # tickerエンドポイントはダッシュ無し表記
        base = c.get("baseTokenId") or symbol.split("-")[0]

        try:
            ticker_list = _get_json(f"{APEX_API}/ticker?symbol={ticker_symbol}").get("data") or []
        except Exception:
            continue
        if not ticker_list:
            continue
        t = ticker_list[0]

        funding_rate = t.get("fundingRate")
        mark_price = t.get("markPrice") or t.get("lastPrice")
        if funding_rate is None or mark_price is None:
            continue
        volume = t.get("turnover24h")

        rows.append(
            {
                "exchange": "ApeX",
                "base_symbol": base,
                "contract_symbol": symbol,
                "quote_symbol": c.get("settleAssetId", "USDT"),
                "funding_apr_pct": float(funding_rate) * (365 * 24) * 100,
                "mark_price": float(mark_price),
                "volume_24h_usd": float(volume) if volume is not None else None,
            }
        )
    return rows


ORDERLY_INFO_API = "https://api.orderly.org/v1/public/info"
ORDERLY_QUERY_API = "https://api.orderly.org/v1/public/query"


def fetch_raydium_perps() -> list[dict]:
    """Raydium Perps(perps.raydium.io)はOrderly Networkの共有CLOBを白ラベル
    展開したもので、Raydium固有の市場一覧を返すAPIは存在しない(ブラウザでの
    実機調査でもWebSocket経由の配信のみでREST捕捉不可と確認済み)。ただし
    /v1/public/info のsymbol命名規則を調べたところ、"PERP_{BASE}_USDC" という
    標準3パート形式(ちょうど80件)と、"PERP_{BASE}_USDC_{broker}"という
    ブローカー専用4パート形式(mythos/alpix/fastx等、他フロントエンド限定の
    株式・合成資産銘柄)に綺麗に分かれている。Raydiumの実UI(perps.raydium.io)で
    実際に選択できた12銘柄(ETH/BTC/SOL/HYPE/MEGA/ORDER/MON/EDGE/M/PENGU/MERL/
    PUMP)を照合したところ全て標準3パート形式側に一致したため、3パート形式のみを
    Raydium向けとして扱う(ブローカー専用4パート形式は除外)。"""
    info = _get_json(ORDERLY_INFO_API)
    funding_periods = {}
    for row in info["data"]["rows"]:
        symbol = row["symbol"]
        if row.get("status") != "ACTIVE":
            continue
        if len(symbol.split("_")) != 3:
            continue  # ブローカー専用銘柄(_mythos等)はRaydiumで選択できないため除外
        funding_periods[symbol] = row["funding_period"]

    summary = _get_json(ORDERLY_QUERY_API, "POST", {"type": "marketSummary"})
    rows = []
    for m in summary["data"]["markets"]:
        symbol = m["symbol"]
        period_hours = funding_periods.get(symbol)
        if period_hours is None:
            continue
        funding_rate = m.get("last_funding_rate")
        mark_price = m.get("mark_price")
        if funding_rate is None or mark_price is None:
            continue
        volume = m.get("24h_amount")
        rows.append(
            {
                "exchange": "Raydium",
                "base_symbol": symbol.split("_")[1],
                "contract_symbol": symbol,
                "quote_symbol": "USDC",
                "funding_apr_pct": float(funding_rate) * (365 * 24 / period_hours) * 100,
                "mark_price": float(mark_price),
                "volume_24h_usd": float(volume) if volume is not None else None,
            }
        )
    return rows


FETCHERS = {
    "Hyperliquid": fetch_hyperliquid_perps,
    "Aster": fetch_aster_perps,
    "Backpack": fetch_backpack_perps,
    "Injective": fetch_injective_perps,
    "edgeX": fetch_edgex_perps,
    "dYdX": fetch_dydx_perps,
    "ApeX": fetch_apex_perps,
    "Raydium": fetch_raydium_perps,
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
            meta = _INJECTIVE_MARKET_META_BY_TICKER.get(contract_symbol)
            if meta is None:
                return None
            history = _get_json(
                f"{INJECTIVE_API}/api/exchange/derivative/v1/fundingRates?marketId={meta['market_id']}&limit=100"
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
) -> tuple[list[dict], list[dict]]:
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

    # 出来高判明済み(tradable)の行はAPR上位N件だけ3日実績を検証すれば十分だが、
    # 出来高不明(参考情報、主にInjective絡み)の行は「本当に実在・取引可能か」を
    # 確定させる必要があるため全件検証する。一部だけ検証すると、キャップ外に
    # 実在しないゴースト市場が紛れ込んだまま表示されてしまう(実際に発生していた)。
    # GitHub Actionsに実行時間の制約は無いため、正確性を優先し全件検証する。
    rows.sort(key=lambda r: r["net_apr_now_pct"], reverse=True)
    tradable_rows = [
        r for r in rows if r["short_volume_24h_usd"] is not None and r["long_volume_24h_usd"] is not None
    ]
    reference_rows = [r for r in rows if r["short_volume_24h_usd"] is None or r["long_volume_24h_usd"] is None]

    to_verify = tradable_rows[:TOP_N_TO_VERIFY] + reference_rows
    verify_ids = {id(r) for r in to_verify}
    rest = [r for r in rows if id(r) not in verify_ids]

    def _strip_note_fragment(note: str, fragment: str) -> str:
        return " / ".join(p for p in note.split(" / ") if p != fragment)

    def _backfill_injective_volume(r: dict, side: str) -> None:
        """side='short'|'long'。Injective脚で出来高が未知なら実際の24h出来高を取得して埋める。"""
        exchange = r[f"{side}_exchange"]
        if exchange != "Injective" or r[f"{side}_volume_24h_usd"] is not None:
            return
        volume, complete = fetch_injective_24h_volume_usd(r[f"{side}_contract"])
        if volume is None:
            return
        r[f"{side}_volume_24h_usd"] = round(volume, 2)
        if not complete:
            r["note"] = (r["note"] + " / " if r["note"] else "") + f"{side}側出来高は取得上限のため過小評価の可能性"
        elif volume < min_liquidity_usd:
            r["note"] = (
                (r["note"] + " / " if r["note"] else "")
                + f"{side}側の実出来高が最小流動性(${min_liquidity_usd:,.0f})未満(${volume:,.0f})"
            )

    # 検証フェーズでInjective脚の実出来高が判明し、最小流動性を下回ることが確定した
    # 行はここでexcludedに回す(市場としては存在するが実質的に執行不能な「ゴースト
    # 市場」がAPR順リストの上位を占めてしまうのを防ぐため。ファンディングキャリー側の
    # 出来高フィルタと同じ考え方)
    excluded = []
    verified = []
    for r in to_verify:
        _backfill_injective_volume(r, "short")
        _backfill_injective_volume(r, "long")
        if r["short_volume_24h_usd"] is not None and r["long_volume_24h_usd"] is not None:
            r["note"] = _strip_note_fragment(r["note"], "出来高不明な脚を含む(参考情報)")

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

        below_threshold = any(
            r[f"{side}_volume_24h_usd"] is not None and r[f"{side}_volume_24h_usd"] < min_liquidity_usd
            for side in ("short", "long")
        )
        if below_threshold:
            excluded.append(r)
        else:
            verified.append(r)

    for r in rest:
        r["short_funding_apr_3d_pct"] = None
        r["long_funding_apr_3d_pct"] = None
        r["net_apr_3d_pct"] = None
        r["note"] = (r["note"] + " / " if r["note"] else "") + "上位候補外のため3日実績は未検証(瞬間値のみ)"

    all_rows = verified + rest

    def sort_key(r):
        # 3日実績で検証済みの行を常に未検証の行より優先する
        # (瞬間値だけの薄い銘柄が非現実的に大きい数値で上位に残るのを防ぐため)
        is_verified = r["net_apr_3d_pct"] is not None
        value = r["net_apr_3d_pct"] if is_verified else r["net_apr_now_pct"]
        return (1 if is_verified else 0, value)

    all_rows.sort(key=sort_key, reverse=True)
    return all_rows, excluded


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
        "--exchanges", default="Hyperliquid,Aster,Backpack,Injective,edgeX,dYdX,ApeX,Raydium"
    )
    args = parser.parse_args()

    exchanges = [e.strip() for e in args.exchanges.split(",") if e.strip()]
    output_path = args.output or f"funding_spread.{args.format}"

    rows, excluded = build_spread_table(args.min_liquidity_usd, exchanges)

    if args.format == "json":
        write_json(rows, output_path)
    else:
        write_csv(rows, output_path)

    print(f"{len(rows)} 件のスプレッド候補を出力しました -> {output_path}", file=sys.stderr)
    if excluded:
        names = ", ".join(f"{e['base_symbol']}({e['short_exchange']}/{e['long_exchange']})" for e in excluded)
        print(
            f"注: 検証の結果、実出来高が最小流動性未満のため除外: {names}",
            file=sys.stderr,
        )
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
