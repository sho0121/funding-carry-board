#!/usr/bin/env python3
"""
実際に建てた裁定ポジションを記録し、各取引所の公開funding履歴API(認証不要)から
実績のfunding損益を自動計算する、ローカル専用の収益管理台帳。

取引所APIキーがまだ無いため発注はユーザーが手動で行う。このツールに必要なのは
「いつ・どの裁定を・いくらで建てたか」を記録することだけで、含み損益/確定損益は
各取引所の公開funding履歴から自動計算する(価格変動によるベーシスP&Lはデルタニュート
ラル戦略の性質上、簡略化のため対象外。funding損益のみを追跡する)。

positions.json は実際の運用金額を含むため .gitignore 対象とし、ローカルにのみ保持する。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone

POSITIONS_PATH = "positions.json"

HL_API = "https://api.hyperliquid.xyz/info"
ASTER_FAPI = "https://fapi.asterdex.com"
BACKPACK_API = "https://api.backpack.exchange"

HISTORY_SUPPORTED_EXCHANGES = {"Hyperliquid", "Aster", "Backpack"}


def _get_json(url: str, method: str = "GET", body: dict | None = None, timeout: int = 20) -> object:
    headers = {"User-Agent": "Mozilla/5.0"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 取引所ごとの funding 履歴合計 (%) 取得。Injective は履歴APIが未特定のため非対応。
# 戻り値: (cumulative_pct, complete) — complete=False は取得できた履歴が
# 指定開始日時まで遡れていない(=集計が不完全)ことを示す
# ---------------------------------------------------------------------------


def fetch_cumulative_funding_pct(
    exchange: str, contract_symbol: str, base_symbol: str, start_ms: int, end_ms: int
) -> tuple[float | None, bool]:
    try:
        if exchange == "Hyperliquid":
            history = _get_json(
                HL_API, "POST", {"type": "fundingHistory", "coin": base_symbol, "startTime": start_ms}
            )
            rates = [float(h["fundingRate"]) for h in history if start_ms <= h["time"] <= end_ms]
            return (sum(rates) * 100 if rates else None), True

        if exchange == "Aster":
            history = _get_json(
                f"{ASTER_FAPI}/fapi/v1/fundingRate?symbol={contract_symbol}"
                f"&startTime={start_ms}&endTime={end_ms}&limit=1000"
            )
            rates = [float(h["fundingRate"]) for h in history]
            return (sum(rates) * 100 if rates else None), True

        if exchange == "Backpack":
            history = _get_json(f"{BACKPACK_API}/api/v1/fundingRates?symbol={contract_symbol}&limit=1000")

            def to_ms(iso: str) -> int:
                return int(time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%S")) * 1000)

            filtered = [h for h in history if start_ms <= to_ms(h["intervalEndTimestamp"]) <= end_ms]
            complete = True
            if history:
                oldest_ms = min(to_ms(h["intervalEndTimestamp"]) for h in history)
                if oldest_ms > start_ms:
                    complete = False  # 取得できた履歴が開始日時まで遡れていない
            rates = [float(h["fundingRate"]) for h in filtered]
            return (sum(rates) * 100 if rates else None), complete

        return None, False  # Injective: 履歴API未対応、手動でP&Lを入力すること
    except Exception as e:
        print(f"警告: {exchange} {contract_symbol} の履歴取得に失敗: {e}", file=sys.stderr)
        return None, False


def compute_position_pnl(position: dict, end_ms: int) -> dict:
    """position を open してから end_ms までの funding P&L(%・USD)を計算する。
    データ不足の脚があれば data_complete=False とし、その旨を呼び出し側に伝える。"""
    start_ms = _iso_to_ms(position["opened_at"])
    notional = position["notional_usd"]
    legs = position["legs"]
    data_complete = True
    total_pct = 0.0
    detail = []

    def add_leg(exchange, contract, receives_when_positive):
        nonlocal total_pct, data_complete
        cum_pct, complete = fetch_cumulative_funding_pct(exchange, contract, position["base_symbol"], start_ms, end_ms)
        if not complete:
            data_complete = False
        if cum_pct is None:
            detail.append({"exchange": exchange, "contract": contract, "cum_pct": None})
            return
        cashflow_pct = cum_pct if receives_when_positive else -cum_pct
        total_pct += cashflow_pct
        detail.append({"exchange": exchange, "contract": contract, "cum_pct": round(cashflow_pct, 4)})

    if position["type"] == "carry":
        add_leg(legs["exchange"], legs["contract"], legs["perp_side"] == "short")
    else:
        add_leg(legs["short_exchange"], legs["short_contract"], True)
        add_leg(legs["long_exchange"], legs["long_contract"], False)

    return {
        "pnl_pct": round(total_pct, 4),
        "pnl_usd": round(notional * total_pct / 100, 2),
        "data_complete": data_complete,
        "legs_detail": detail,
    }


# ---------------------------------------------------------------------------
# 台帳の読み書き
# ---------------------------------------------------------------------------


def load_positions(path: str = POSITIONS_PATH) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_positions(positions: list[dict], path: str = POSITIONS_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_open(args) -> None:
    positions = load_positions()
    pos_id = uuid.uuid4().hex[:8]

    if args.type == "carry":
        if not (args.exchange and args.contract and args.perp_side):
            sys.exit("carry型には --exchange --contract --perp-side が必要です")
        legs = {"exchange": args.exchange, "contract": args.contract, "perp_side": args.perp_side}
        if args.exchange not in HISTORY_SUPPORTED_EXCHANGES:
            print(f"注意: {args.exchange} は履歴自動取得に対応していません(closeまたはstatusでP&Nを手動入力してください)", file=sys.stderr)
    else:
        if not (args.short_exchange and args.short_contract and args.long_exchange and args.long_contract):
            sys.exit("spread型には --short-exchange --short-contract --long-exchange --long-contract が必要です")
        legs = {
            "short_exchange": args.short_exchange,
            "short_contract": args.short_contract,
            "long_exchange": args.long_exchange,
            "long_contract": args.long_contract,
        }

    position = {
        "id": pos_id,
        "type": args.type,
        "base_symbol": args.base,
        "notional_usd": args.notional,
        "opened_at": args.opened_at or _now_iso(),
        "status": "open",
        "closed_at": None,
        "legs": legs,
        "notes": args.notes or "",
        "realized_pnl_usd": None,
    }
    positions.append(position)
    save_positions(positions)
    print(f"ポジションを記録しました: id={pos_id} {args.type} {args.base} ${args.notional:,.0f}")


def cmd_close(args) -> None:
    positions = load_positions()
    position = next((p for p in positions if p["id"] == args.id), None)
    if position is None:
        sys.exit(f"id={args.id} のポジションが見つかりません")
    if position["status"] == "closed":
        sys.exit(f"id={args.id} は既にクローズ済みです")

    closed_at = args.closed_at or _now_iso()
    if args.manual_pnl is not None:
        realized = args.manual_pnl
        note = "(手動入力)"
    else:
        result = compute_position_pnl(position, _iso_to_ms(closed_at))
        realized = result["pnl_usd"]
        note = "" if result["data_complete"] else "(注意: 一部履歴データが不完全な可能性)"

    position["status"] = "closed"
    position["closed_at"] = closed_at
    position["realized_pnl_usd"] = realized
    if args.notes:
        position["notes"] = (position["notes"] + " / " if position["notes"] else "") + args.notes
    save_positions(positions)
    print(f"クローズしました: id={args.id} 確定P&L=${realized:,.2f} {note}")


def cmd_status(args) -> None:
    positions = load_positions()
    open_positions = [p for p in positions if p["status"] == "open"]
    if not open_positions:
        print("オープン中のポジションはありません")
        return

    now_ms = int(time.time() * 1000)
    total_unrealized = 0.0
    for p in open_positions:
        result = compute_position_pnl(p, now_ms)
        total_unrealized += result["pnl_usd"]
        days = (now_ms - _iso_to_ms(p["opened_at"])) / 86400000
        flag = "" if result["data_complete"] else " ⚠データ不完全"
        print(
            f"[{p['id']}] {p['type']} {p['base_symbol']} ${p['notional_usd']:,.0f} "
            f"| 保有{days:.1f}日 | 含み損益 ${result['pnl_usd']:,.2f} ({result['pnl_pct']:.3f}%){flag}"
        )
    print(f"\n含み損益合計: ${total_unrealized:,.2f} ({len(open_positions)}件)")


def cmd_summary(args) -> None:
    positions = load_positions()
    closed = [p for p in positions if p["status"] == "closed"]
    open_positions = [p for p in positions if p["status"] == "open"]

    realized_total = sum(p["realized_pnl_usd"] for p in closed)
    wins = sum(1 for p in closed if p["realized_pnl_usd"] > 0)
    losses = sum(1 for p in closed if p["realized_pnl_usd"] <= 0)

    now_ms = int(time.time() * 1000)
    unrealized_total = sum(compute_position_pnl(p, now_ms)["pnl_usd"] for p in open_positions)

    print(f"確定損益合計: ${realized_total:,.2f} ({len(closed)}件 / 勝ち{wins} 負け{losses})")
    print(f"含み損益合計: ${unrealized_total:,.2f} ({len(open_positions)}件オープン中)")
    print(f"合計: ${realized_total + unrealized_total:,.2f}")

    if closed:
        best = max(closed, key=lambda p: p["realized_pnl_usd"])
        worst = min(closed, key=lambda p: p["realized_pnl_usd"])
        print(f"最良: [{best['id']}] {best['base_symbol']} ${best['realized_pnl_usd']:,.2f}")
        print(f"最悪: [{worst['id']}] {worst['base_symbol']} ${worst['realized_pnl_usd']:,.2f}")


def main():
    parser = argparse.ArgumentParser(description="裁定ポジションの収益管理(ローカル台帳)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="ポジションを記録する")
    p_open.add_argument("--type", choices=["carry", "spread"], required=True)
    p_open.add_argument("--base", required=True, help="ベースシンボル (例: BTC)")
    p_open.add_argument("--notional", type=float, required=True)
    p_open.add_argument("--exchange", help="carry型: 取引所")
    p_open.add_argument("--contract", help="carry型: perpのcontract symbol")
    p_open.add_argument("--perp-side", choices=["short", "long"], help="carry型: perpの方向")
    p_open.add_argument("--short-exchange", help="spread型: ショート側取引所")
    p_open.add_argument("--short-contract", help="spread型: ショート側contract symbol")
    p_open.add_argument("--long-exchange", help="spread型: ロング側取引所")
    p_open.add_argument("--long-contract", help="spread型: ロング側contract symbol")
    p_open.add_argument("--opened-at", help="ISO日時 (省略時は現在時刻)")
    p_open.add_argument("--notes", default="")
    p_open.set_defaults(func=cmd_open)

    p_close = sub.add_parser("close", help="ポジションをクローズし確定P&Lを記録する")
    p_close.add_argument("--id", required=True)
    p_close.add_argument("--closed-at", help="ISO日時 (省略時は現在時刻)")
    p_close.add_argument("--manual-pnl", type=float, help="自動計算の代わりに手動でP&L(USD)を指定")
    p_close.add_argument("--notes", default="")
    p_close.set_defaults(func=cmd_close)

    p_status = sub.add_parser("status", help="オープン中のポジションと含み損益を表示する")
    p_status.set_defaults(func=cmd_status)

    p_summary = sub.add_parser("summary", help="確定・含み損益の集計を表示する")
    p_summary.set_defaults(func=cmd_summary)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
