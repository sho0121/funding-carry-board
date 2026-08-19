#!/usr/bin/env python3
"""
Funding Carry Board が対象とする取引所(Hyperliquid, Aster, Backpack, Injective)に
関連するリスクイベント(ハッキング等)と、トレンド銘柄(将来的な新規上場候補の目安)を
認証不要の公開APIから収集する。

GitHub Actions の毎時ジョブ(LLM呼び出し不可)からも実行できるよう、純Pythonのみで
完結させる。定性的な深掘り("この取引所は本当に注意すべきか"等)は自動化せず、
market-intel-analyst サブエージェント(WebSearchあり)がオンデマンドで
market_intel_notes.md に追記する運用とする。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

DEFILLAMA_HACKS_URL = "https://api.llama.fi/hacks"
COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"

HACK_LOOKBACK_DAYS = 90
TRACKED_EXCHANGES = ["Hyperliquid", "Aster", "Backpack", "Injective"]


def _get_json(url: str, timeout: int = 20) -> object:
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _related_exchange(hack: dict) -> str | None:
    """名称の完全一致(大文字小文字無視)のみを対象とする。取引所名を含むだけの
    無関係な別プロジェクト("Hyperliquid Malaysia"等)を誤検知しないよう、
    部分一致やチェーン名での判定はあえて行わない。"""
    name = (hack.get("name") or "").strip().lower()
    for ex in TRACKED_EXCHANGES:
        if name == ex.lower():
            return ex
    return None


def fetch_risk_events() -> list[dict]:
    try:
        hacks = _get_json(DEFILLAMA_HACKS_URL)
    except Exception as e:
        return [{"error": f"DeFiLlama hacks取得失敗: {e}"}]

    cutoff = time.time() - HACK_LOOKBACK_DAYS * 86400
    events = []
    for h in hacks:
        ts = h.get("date")
        if ts is None or ts < cutoff:
            continue
        related = _related_exchange(h)
        if related is None:
            continue
        events.append(
            {
                "exchange": related,
                "name": h.get("name"),
                "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                "amount_usd": h.get("amount"),
                "classification": h.get("classification"),
                "technique": h.get("technique"),
                "chain": h.get("chain"),
            }
        )
    events.sort(key=lambda e: e["date"], reverse=True)
    return events


def fetch_trending_coins(limit: int = 10) -> list[dict]:
    try:
        data = _get_json(COINGECKO_TRENDING_URL)
    except Exception as e:
        return [{"error": f"CoinGeckoトレンド取得失敗: {e}"}]

    coins = []
    for entry in (data.get("coins") or [])[:limit]:
        item = entry.get("item", {})
        coins.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "market_cap_rank": item.get("market_cap_rank"),
            }
        )
    return coins


def load_analyst_notes(path: str = "market_intel_notes.md") -> str | None:
    """market-intel-analyst サブエージェントが書き溜めた定性メモ。あれば末尾を添える。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def fetch_market_intel() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": HACK_LOOKBACK_DAYS,
        "risk_events": fetch_risk_events(),
        "trending_coins": fetch_trending_coins(),
        "analyst_notes_available": load_analyst_notes() is not None,
    }


def write_json(payload: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="市場インテリジェンス(リスクイベント・トレンド銘柄)を収集する")
    parser.add_argument("-o", "--output", default="market_intel.json")
    args = parser.parse_args()

    payload = fetch_market_intel()
    write_json(payload, args.output)

    print(
        f"リスクイベント {len(payload['risk_events'])} 件、トレンド銘柄 {len(payload['trending_coins'])} 件 -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
