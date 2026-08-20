#!/usr/bin/env python3
"""
「エッジ・ラボ」事業部の自動監視(キャッチ)部分。edge_playbook.md にカタログ化された
エッジのうち、認証不要の公開データだけで機械的に検知できるものだけを対象にする
(market_intel.py と同じ設計思想: 純Python・LLM呼び出し不可のGitHub Actionsから実行可能)。

対応シグナル:
  1. 新規上場検知: funding_spread_scanner.py の各取引所フェッチャーが返す現在の銘柄
     一覧を、前回実行時のスナップショット(edge_watch_snapshot.json)と比較し、新規に
     現れた契約を検出する(追加のAPI呼び出しなしで実現できる)
  2. 異常な価格乖離: multi_exchange_arbitrage.py のcarry候補のうち、spot/perp間の
     ベーシスが閾値を超えているものを抽出する(既に取得済みのデータから追加コストなし)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from funding_spread_scanner import FETCHERS

SNAPSHOT_PATH = "edge_watch_snapshot.json"
SIGNALS_PATH = "edge_signals.json"

EXTREME_BASIS_PCT_THRESHOLD = 1.5  # spot/perp価格乖離がこの%を超えたら異常として拾う


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_universe() -> dict[str, set[str]]:
    """各取引所の現在の全perp契約一覧(dual-listed等の絞り込み前、生の全銘柄)を返す。"""
    universe: dict[str, set[str]] = {}
    for exchange, fetcher in FETCHERS.items():
        try:
            rows = fetcher()
            universe[exchange] = {r["contract_symbol"] for r in rows}
        except Exception as e:
            print(f"警告: {exchange} の銘柄一覧取得に失敗: {e}", file=sys.stderr)
            universe[exchange] = set()
    return universe


def load_snapshot(path: str = SNAPSHOT_PATH) -> dict[str, list[str]]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_snapshot(universe: dict[str, set[str]], path: str = SNAPSHOT_PATH) -> None:
    serializable = {ex: sorted(symbols) for ex, symbols in universe.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2)


def detect_new_listings() -> list[dict]:
    """前回スナップショットとの差分で新規上場を検出する。取得失敗した取引所は比較対象から
    除外し(誤って全銘柄を「新規」と誤検知しないため)、スナップショットも更新しない。
    初回実行(前回データが無い取引所)はベースライン保存のみで新規扱いにはしない。"""
    current = current_universe()
    previous = load_snapshot()

    new_listings = []
    updated_snapshot = dict(previous)
    for exchange, symbols in current.items():
        if not symbols:
            continue  # 取得失敗: 前回のスナップショットをそのまま維持する
        prev_symbols = set(previous.get(exchange, []))
        if prev_symbols:
            for sym in sorted(symbols - prev_symbols):
                new_listings.append({"exchange": exchange, "contract_symbol": sym, "detected_at": _now_iso()})
        updated_snapshot[exchange] = symbols

    save_snapshot(updated_snapshot)
    return new_listings


def detect_extreme_basis(carry_rows: list[dict]) -> list[dict]:
    """spot/perpのベーシス乖離が閾値を超えている行を抽出する(既存データの再利用のみ)。"""
    extreme = []
    for row in carry_rows:
        basis = row.get("basis_pct")
        if basis is None:
            continue
        if abs(basis) >= EXTREME_BASIS_PCT_THRESHOLD:
            extreme.append(
                {
                    "exchange": row["exchange"],
                    "perp_symbol": row["perp_symbol"],
                    "basis_pct": round(basis, 3),
                }
            )
    extreme.sort(key=lambda e: abs(e["basis_pct"]), reverse=True)
    return extreme


def fetch_edge_signals(carry_rows: list[dict] | None = None) -> dict:
    """generate_dashboard.py から呼ばれる場合は carry_rows を渡して二重フェッチを避ける。
    単独実行(CLI)の場合は None のままで、新規上場検知のみ行う
    (異常ベーシス検知にはcarry_rowsが必要なため)。"""
    new_listings = detect_new_listings()
    extreme_basis = detect_extreme_basis(carry_rows) if carry_rows is not None else []
    return {
        "generated_at": _now_iso(),
        "new_listings": new_listings,
        "extreme_basis": extreme_basis,
    }


def write_json(payload: dict, path: str = SIGNALS_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="エッジ・ラボの自動監視(新規上場検知・異常ベーシス検知)")
    parser.add_argument("-o", "--output", default=SIGNALS_PATH)
    args = parser.parse_args()

    payload = fetch_edge_signals()
    write_json(payload, args.output)

    print(
        f"新規上場 {len(payload['new_listings'])} 件、異常ベーシス {len(payload['extreme_basis'])} 件 -> {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
