#!/usr/bin/env python3
"""
裁定取引ペーパートレードBot。risk_manager.py のスコアリングロジックを使い、
人間の代わりに機械的に「発注判断」を行うが、実際の発注は一切しない。
Hyperliquid/Aster/Backpack/Injectiveの公開funding履歴API(認証不要)から実際の値動きを
追跡し、「もし本当に発注していたらどうなっていたか」を検証するためのツール。

portfolio.py の positions.json(実運用・実金額を含むため非公開)とは別に、
paper_positions.json を使う。こちらはシミュレーションのみで実金額を含まないため、
公開・git管理してよい(ダッシュボードにも表示する)。

サイジング・リスク評価は risk_manager.py の各関数(normalize_carry_row 等)をそのまま
再利用し、損益計算は portfolio.py の compute_position_pnl をそのまま再利用する。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone

from risk_manager import (
    TOTAL_CAPITAL_USD,
    MAX_POSITION_PCT_OF_CAPITAL,
    MAX_EXCHANGE_PCT_OF_CAPITAL,
    MAX_BASE_PCT_OF_CAPITAL,
    MAX_VOLUME_PARTICIPATION_PCT,
    normalize_carry_row,
    normalize_spread_row,
    score_opportunity,
    load_intel_exchange_flags,
)
from portfolio import compute_position_pnl

PAPER_POSITIONS_PATH = "paper_positions.json"

MIN_ENTRY_APR_PCT = 5.0  # このAPR未満の候補は自動エントリーしない(ノイズ回避)
MAX_HOLD_DAYS = 14.0  # 保有上限日数(理論が古びるのを防ぐ安全弁)
FEE_PCT_PER_LEG_ONE_WAY = 0.05  # 1脚・片道あたりのテイカー手数料想定(%)
MAX_OPEN_POSITIONS = 15  # 同時保有件数の上限(過度な分散/チャーン防止)
MIN_POSITION_USD = 50.0  # これ未満のサイズは手数料で意味が無いため開かない


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def load_paper_positions(path: str = PAPER_POSITIONS_PATH) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def save_paper_positions(positions: list[dict], path: str = PAPER_POSITIONS_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# carry行/spread行 -> スコアリング済みopportunity (+ portfolio.py互換のlegs)
# ---------------------------------------------------------------------------


def _legs_from_carry_row(row: dict) -> dict:
    perp_side = "short" if "ショート" in (row.get("perp_action") or "") else "long"
    return {
        "exchange": row["exchange"],
        "contract": row.get("perp_contract_symbol") or row["perp_symbol"],
        "perp_side": perp_side,
    }


def _legs_from_spread_row(row: dict) -> dict:
    return {
        "short_exchange": row["short_exchange"],
        "short_contract": row["short_contract"],
        "long_exchange": row["long_exchange"],
        "long_contract": row["long_contract"],
    }


def _opp_signature(type_: str, legs: dict) -> tuple:
    if type_ == "carry":
        return ("carry", legs["exchange"], legs["contract"])
    return ("spread", legs["short_exchange"], legs["short_contract"], legs["long_exchange"], legs["long_contract"])


def _build_candidates(carry_rows: list[dict], spread_rows: list[dict], intel_exchanges: set[str]) -> list[dict]:
    candidates = []
    for row in carry_rows:
        opp = normalize_carry_row(row)
        if opp is None:
            continue
        score_opportunity(opp, intel_exchanges)
        opp["_legs"] = _legs_from_carry_row(row)
        opp["_signature"] = _opp_signature("carry", opp["_legs"])
        candidates.append(opp)
    for row in spread_rows:
        opp = normalize_spread_row(row)
        if opp is None:
            continue
        score_opportunity(opp, intel_exchanges)
        opp["_legs"] = _legs_from_spread_row(row)
        opp["_signature"] = _opp_signature("spread", opp["_legs"])
        candidates.append(opp)
    return candidates


# ---------------------------------------------------------------------------
# 1サイクル実行: 既存ポジションの再評価・エグジット → 新規エントリー
# ---------------------------------------------------------------------------


def run_cycle(
    carry_rows: list[dict],
    spread_rows: list[dict],
    capital_usd: float = TOTAL_CAPITAL_USD,
    path: str = PAPER_POSITIONS_PATH,
) -> dict:
    positions = load_paper_positions(path)
    intel_exchanges = load_intel_exchange_flags()
    candidates = _build_candidates(carry_rows, spread_rows, intel_exchanges)
    candidates_by_sig = {c["_signature"]: c for c in candidates}

    now_ms = int(time.time() * 1000)
    now_iso = _now_iso()

    # 1. 既存オープンポジションの再評価・エグジット判定
    just_closed_signatures = set()
    for pos in positions:
        if pos["status"] != "open":
            continue
        sig = _opp_signature(pos["type"], pos["legs"])
        matching = candidates_by_sig.get(sig)
        held_days = (now_ms - _iso_to_ms(pos["opened_at"])) / 86400000

        close_reason = None
        if matching is None:
            close_reason = "対象の裁定機会が消滅"
        elif matching["apr_pct"] <= 0:
            close_reason = "net APRがマイナスに転じた"
        elif held_days >= MAX_HOLD_DAYS:
            close_reason = f"最大保有期間({MAX_HOLD_DAYS:.0f}日)に到達"

        if close_reason:
            result = compute_position_pnl(pos, now_ms)
            num_legs = 2 if pos["type"] == "spread" else 1
            fee_drag_usd = pos["notional_usd"] * (FEE_PCT_PER_LEG_ONE_WAY / 100) * 2 * num_legs
            pos["status"] = "closed"
            pos["closed_at"] = now_iso
            pos["realized_pnl_usd"] = round(result["pnl_usd"] - fee_drag_usd, 2)
            pos["notes"] = (pos["notes"] + " / " if pos["notes"] else "") + f"自動クローズ: {close_reason}"
            just_closed_signatures.add(sig)

    # 2. 残り資金枠の計算 (現時点でオープン中の全ポジション基準)
    open_positions = [p for p in positions if p["status"] == "open"]
    allocated_total = sum(p["notional_usd"] for p in open_positions)
    allocated_by_exchange: dict[str, float] = {}
    allocated_by_base: dict[str, float] = {}
    held_signatures = set()
    for p in open_positions:
        held_signatures.add(_opp_signature(p["type"], p["legs"]))
        exs = (
            [p["legs"]["exchange"]]
            if p["type"] == "carry"
            else [p["legs"]["short_exchange"], p["legs"]["long_exchange"]]
        )
        for ex in exs:
            allocated_by_exchange[ex] = allocated_by_exchange.get(ex, 0.0) + p["notional_usd"]
        allocated_by_base[p["base_symbol"]] = allocated_by_base.get(p["base_symbol"], 0.0) + p["notional_usd"]

    max_position = capital_usd * MAX_POSITION_PCT_OF_CAPITAL
    max_per_exchange = capital_usd * MAX_EXCHANGE_PCT_OF_CAPITAL
    max_per_base = capital_usd * MAX_BASE_PCT_OF_CAPITAL

    # 3. 新規エントリー判定: 検証済み(verified)・最低APR以上・未保有の候補のみ対象
    blocked_signatures = held_signatures | just_closed_signatures
    eligible = [
        o
        for o in candidates
        if o["verified"] and o["apr_pct"] >= MIN_ENTRY_APR_PCT and o["_signature"] not in blocked_signatures
    ]
    eligible.sort(key=lambda o: o["apr_pct"] * o["risk_multiplier"], reverse=True)

    opened_count = 0
    for opp in eligible:
        if len(open_positions) + opened_count >= MAX_OPEN_POSITIONS:
            break

        exs = (
            [opp["_legs"]["exchange"]]
            if opp["opportunity_type"] == "carry"
            else [opp["_legs"]["short_exchange"], opp["_legs"]["long_exchange"]]
        )
        remaining_total = capital_usd - allocated_total
        remaining_exchange = min(max_per_exchange - allocated_by_exchange.get(ex, 0.0) for ex in exs)
        remaining_base = max_per_base - allocated_by_base.get(opp["base_symbol"], 0.0)
        cap_candidates = [max_position, remaining_total, remaining_exchange, remaining_base]
        if opp["volume_usd"] is not None:
            cap_candidates.append(opp["volume_usd"] * MAX_VOLUME_PARTICIPATION_PCT)
        size = round(max(0.0, min(cap_candidates)), 2)
        if size < MIN_POSITION_USD:
            continue

        new_pos = {
            "id": uuid.uuid4().hex[:8],
            "type": opp["opportunity_type"],
            "base_symbol": opp["base_symbol"],
            "notional_usd": size,
            "opened_at": now_iso,
            "status": "open",
            "closed_at": None,
            "legs": opp["_legs"],
            "notes": f"Bot自動エントリー: APR {opp['apr_pct']:.2f}% / リスク倍率 {opp['risk_multiplier']:.2f}",
            "realized_pnl_usd": None,
        }
        positions.append(new_pos)
        opened_count += 1

        allocated_total += size
        for ex in exs:
            allocated_by_exchange[ex] = allocated_by_exchange.get(ex, 0.0) + size
        allocated_by_base[opp["base_symbol"]] = allocated_by_base.get(opp["base_symbol"], 0.0) + size

    save_paper_positions(positions, path)
    return build_summary_payload(positions, capital_usd, now_ms)


# ---------------------------------------------------------------------------
# ダッシュボード/CLI向けサマリー
# ---------------------------------------------------------------------------


def build_summary_payload(positions: list[dict], capital_usd: float, now_ms: int) -> dict:
    open_positions = [p for p in positions if p["status"] == "open"]
    closed_positions = [p for p in positions if p["status"] == "closed"]

    open_rows = []
    unrealized_total = 0.0
    for p in open_positions:
        result = compute_position_pnl(p, now_ms)
        unrealized_total += result["pnl_usd"]
        open_rows.append(
            {
                "id": p["id"],
                "type": p["type"],
                "base_symbol": p["base_symbol"],
                "notional_usd": p["notional_usd"],
                "opened_at": p["opened_at"],
                "pnl_usd": result["pnl_usd"],
                "pnl_pct": result["pnl_pct"],
                "data_complete": result["data_complete"],
                "notes": p["notes"],
            }
        )
    open_rows.sort(key=lambda r: r["pnl_usd"], reverse=True)

    realized_total = sum(p["realized_pnl_usd"] for p in closed_positions)
    wins = sum(1 for p in closed_positions if p["realized_pnl_usd"] > 0)
    losses = sum(1 for p in closed_positions if p["realized_pnl_usd"] <= 0)

    recent_closed = sorted(closed_positions, key=lambda p: p["closed_at"], reverse=True)[:20]
    closed_rows = [
        {
            "id": p["id"],
            "type": p["type"],
            "base_symbol": p["base_symbol"],
            "notional_usd": p["notional_usd"],
            "opened_at": p["opened_at"],
            "closed_at": p["closed_at"],
            "realized_pnl_usd": p["realized_pnl_usd"],
            "notes": p["notes"],
        }
        for p in recent_closed
    ]

    first_opened = min((p["opened_at"] for p in positions), default=None)
    days_running = round((now_ms - _iso_to_ms(first_opened)) / 86400000, 1) if first_opened else 0.0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "capital_usd": capital_usd,
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "unrealized_pnl_usd": round(unrealized_total, 2),
        "realized_pnl_usd": round(realized_total, 2),
        "total_pnl_usd": round(unrealized_total + realized_total, 2),
        "win_count": wins,
        "loss_count": losses,
        "win_rate_pct": round(100 * wins / (wins + losses), 1) if (wins + losses) else None,
        "days_running": days_running,
        "open_positions": open_rows,
        "recent_closed": closed_rows,
    }


# ---------------------------------------------------------------------------
# CLI (単独実行・デバッグ用)
# ---------------------------------------------------------------------------


def cmd_run(args) -> None:
    from multi_exchange_arbitrage import build_combined_table
    from funding_spread_scanner import build_spread_table

    carry_exchanges = ["hyperliquid", "aster", "backpack", "injective"]  # spotが無いedgeXは対象外
    spread_exchanges = ["Hyperliquid", "Aster", "Backpack", "Injective", "edgeX", "dYdX", "ApeX"]
    carry_rows, _, _ = build_combined_table(10000.0, 20000.0, carry_exchanges)
    spread_rows, _ = build_spread_table(20000.0, spread_exchanges)

    summary = run_cycle(carry_rows, spread_rows, args.capital_usd)
    print(
        f"オープン中: {summary['open_count']}件 (含み損益 ${summary['unrealized_pnl_usd']:,.2f}) / "
        f"クローズ済み: {summary['closed_count']}件 (確定損益 ${summary['realized_pnl_usd']:,.2f}) / "
        f"合計 ${summary['total_pnl_usd']:,.2f}",
        file=sys.stderr,
    )


def cmd_status(args) -> None:
    positions = load_paper_positions()
    open_positions = [p for p in positions if p["status"] == "open"]
    if not open_positions:
        print("オープン中のペーパーポジションはありません")
        return
    now_ms = int(time.time() * 1000)
    for p in open_positions:
        result = compute_position_pnl(p, now_ms)
        days = (now_ms - _iso_to_ms(p["opened_at"])) / 86400000
        print(
            f"[{p['id']}] {p['type']} {p['base_symbol']} ${p['notional_usd']:,.0f} "
            f"| 保有{days:.1f}日 | 含み損益 ${result['pnl_usd']:,.2f} ({result['pnl_pct']:.3f}%)"
        )


def cmd_summary(args) -> None:
    positions = load_paper_positions()
    now_ms = int(time.time() * 1000)
    summary = build_summary_payload(positions, TOTAL_CAPITAL_USD, now_ms)
    print(f"確定損益: ${summary['realized_pnl_usd']:,.2f} (勝ち{summary['win_count']} 負け{summary['loss_count']})")
    print(f"含み損益: ${summary['unrealized_pnl_usd']:,.2f} ({summary['open_count']}件オープン中)")
    print(f"合計: ${summary['total_pnl_usd']:,.2f} (稼働{summary['days_running']}日)")


def main():
    parser = argparse.ArgumentParser(description="裁定取引ペーパートレードBot(実弾なし・シミュレーション専用)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="1サイクル実行(エグジット判定→エントリー判定)")
    p_run.add_argument("--capital-usd", type=float, default=TOTAL_CAPITAL_USD)
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="オープン中ポジションと含み損益を表示")
    p_status.set_defaults(func=cmd_status)

    p_summary = sub.add_parser("summary", help="確定+含み損益の集計を表示")
    p_summary.set_defaults(func=cmd_summary)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
