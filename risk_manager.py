#!/usr/bin/env python3
"""
ファンディングキャリー(carry)・perp対perp差分(spread)の両候補を横断して、
リスク・出来高・資金配分制約を踏まえた「今取るべき最適な裁定」をランキングする。

多数の候補をAPR順に並べるだけでは、出来高不足で実際には建てられない・送金リスクが
ある・直近リスクイベントのあった取引所が絡む、といった「実行可能性」を無視してしまう。
本モジュールはそれらを織り込んだ composite_score で並べ替え、さらに総資金・取引所別・
銘柄別の上限を守りながら、上位候補から順に資金を割り当てるポートフォリオ構築まで行う。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

# ---------------------------------------------------------------------------
# 資金配分ルール (必要に応じてここを調整する。単位はUSD/比率)
# ---------------------------------------------------------------------------

TOTAL_CAPITAL_USD = 10000.0
MAX_POSITION_PCT_OF_CAPITAL = 0.20  # 1ポジションあたり総資金の最大比率
MAX_EXCHANGE_PCT_OF_CAPITAL = 0.40  # 1取引所あたり総資金の最大比率(送金・障害リスクの分散)
MAX_BASE_PCT_OF_CAPITAL = 0.25  # 1銘柄(ベース資産)あたり総資金の最大比率
MAX_VOLUME_PARTICIPATION_PCT = 0.01  # 24h出来高に対するポジションサイズの上限比率
TOP_N_RECOMMENDED = 10

CARRY_CSV_DEFAULT = "multi_exchange_arbitrage.csv"
SPREAD_CSV_DEFAULT = "funding_spread.csv"
OUTPUT_CSV_DEFAULT = "risk_assessed_opportunities.csv"
INTEL_NOTES_PATH = "market_intel_notes.md"


# ---------------------------------------------------------------------------
# carry行 / spread行 を共通スキーマに正規化する
#   {opportunity_type, label, exchanges, base_symbol, apr_pct, verified,
#    volume_usd, risk_flags, note}
# ---------------------------------------------------------------------------


def _to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_carry_row(row: dict) -> dict | None:
    apr = _to_float(row.get("funding_3d_apr_pct"))
    verified = apr is not None
    if apr is None:
        apr = _to_float(row.get("funding_now_apr_pct"))
    if apr is None or apr <= 0:
        return None

    volume = _to_float(row.get("spot_volume_24h_usd"))
    risk_flags = []
    if row.get("match_type") and row["match_type"] != "exact":
        risk_flags.append(f"銘柄対応が推定ベース({row['match_type']})")
    if row.get("note"):
        risk_flags.append(row["note"])

    return {
        "opportunity_type": "carry",
        "label": f"{row.get('perp_symbol')} [{row.get('exchange')}] 現物{row.get('spot_action')} + perp{row.get('perp_action')}",
        "exchanges": [row.get("exchange")],
        "base_symbol": row.get("perp_symbol"),
        "apr_pct": apr,
        "verified": verified,
        "volume_usd": volume,
        "cross_exchange": False,
        "risk_flags": [f for f in risk_flags if f],
    }


def normalize_spread_row(row: dict) -> dict | None:
    apr = _to_float(row.get("net_apr_3d_pct"))
    verified = apr is not None
    if apr is None:
        apr = _to_float(row.get("net_apr_now_pct"))
    if apr is None or apr <= 0:
        return None

    short_vol = _to_float(row.get("short_volume_24h_usd"))
    long_vol = _to_float(row.get("long_volume_24h_usd"))
    volume = min([v for v in (short_vol, long_vol) if v is not None], default=None)
    if short_vol is None or long_vol is None:
        volume = None  # 片脚でも出来高不明なら参考情報扱い(下でreference_onlyフラグ)

    cross_exchange = row.get("spread_type") == "cross_exchange"
    risk_flags = []
    if row.get("note"):
        risk_flags.append(row["note"])

    return {
        "opportunity_type": "spread",
        "label": (
            f"{row.get('base_symbol')} ショート[{row.get('short_exchange')}] "
            f"/ ロング[{row.get('long_exchange')}]"
        ),
        "exchanges": sorted({row.get("short_exchange"), row.get("long_exchange")}),
        "base_symbol": row.get("base_symbol"),
        "apr_pct": apr,
        "verified": verified,
        "volume_usd": volume,
        "cross_exchange": cross_exchange,
        "risk_flags": [f for f in risk_flags if f],
    }


def load_intel_exchange_flags(path: str = INTEL_NOTES_PATH) -> set[str]:
    """market_intel_notes.md (market-intel-analystエージェントが追記) から、
    取引所名を簡易キーワード一致で拾う。ファイルが無ければ空集合を返す。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return set()

    known_exchanges = ["Hyperliquid", "Aster", "Backpack", "Injective"]
    return {ex for ex in known_exchanges if ex.lower() in text.lower()}


# ---------------------------------------------------------------------------
# スコアリング + ポートフォリオ構築
# ---------------------------------------------------------------------------


def _append_flag_if_new(flags: list[str], keyword: str, phrase: str) -> None:
    """同趣旨のフラグ(スキャナーのnoteに既出等)が無い場合のみ追加する"""
    if not any(keyword in f for f in flags):
        flags.append(phrase)


def score_opportunity(opp: dict, intel_exchanges: set[str]) -> dict:
    risk_multiplier = 1.0
    flags = list(opp["risk_flags"])

    if not opp["verified"]:
        risk_multiplier *= 0.6
        _append_flag_if_new(flags, "3日実績", "3日実績データなし(瞬間値のみ)")
    if opp["cross_exchange"]:
        risk_multiplier *= 0.85
        _append_flag_if_new(flags, "送金", "取引所間送金リスクあり")
    if opp["volume_usd"] is None:
        risk_multiplier *= 0.5
        _append_flag_if_new(flags, "出来高不明", "出来高不明な脚を含む(参考情報)")

    matched_intel = intel_exchanges & set(opp["exchanges"])
    if matched_intel:
        risk_multiplier *= 0.4
        flags.append(f"直近の市場インテリジェンスで注意フラグ({', '.join(sorted(matched_intel))})")

    opp["risk_multiplier"] = round(risk_multiplier, 4)
    opp["risk_flags"] = flags
    return opp


def build_portfolio(
    opportunities: list[dict],
    capital_usd: float = TOTAL_CAPITAL_USD,
) -> list[dict]:
    """出来高上限・資金配分ルールを守りながら、リスク調整後の期待利益が大きい順に
    貪欲法で資金を割り当てる。1つのポジションに複数の制約(総資金/取引所別/銘柄別/
    出来高)の中で最も厳しいものが適用される。"""

    max_position = capital_usd * MAX_POSITION_PCT_OF_CAPITAL
    max_per_exchange = capital_usd * MAX_EXCHANGE_PCT_OF_CAPITAL
    max_per_base = capital_usd * MAX_BASE_PCT_OF_CAPITAL

    # 一旦「上限いっぱいまで建てられたら」という前提の期待利益でソートする
    for opp in opportunities:
        provisional_cap = max_position
        if opp["volume_usd"] is not None:
            provisional_cap = min(provisional_cap, opp["volume_usd"] * MAX_VOLUME_PARTICIPATION_PCT)
        opp["_provisional_score"] = provisional_cap * (opp["apr_pct"] / 100.0) * opp["risk_multiplier"]

    opportunities.sort(key=lambda o: o["_provisional_score"], reverse=True)

    allocated_total = 0.0
    allocated_by_exchange: dict[str, float] = {}
    allocated_by_base: dict[str, float] = {}

    for opp in opportunities:
        remaining_total = capital_usd - allocated_total
        remaining_exchange = min(
            max_per_exchange - allocated_by_exchange.get(ex, 0.0) for ex in opp["exchanges"]
        )
        remaining_base = max_per_base - allocated_by_base.get(opp["base_symbol"], 0.0)

        cap_candidates = [max_position, remaining_total, remaining_exchange, remaining_base]
        if opp["volume_usd"] is not None:
            cap_candidates.append(opp["volume_usd"] * MAX_VOLUME_PARTICIPATION_PCT)

        recommended = max(0.0, min(cap_candidates))
        recommended = round(recommended, 2)

        opp["recommended_position_usd"] = recommended
        opp["est_annual_profit_usd"] = round(recommended * opp["apr_pct"] / 100.0, 2)
        opp["composite_score"] = round(recommended * (opp["apr_pct"] / 100.0) * opp["risk_multiplier"], 2)

        if recommended > 0:
            allocated_total += recommended
            for ex in opp["exchanges"]:
                allocated_by_exchange[ex] = allocated_by_exchange.get(ex, 0.0) + recommended
            allocated_by_base[opp["base_symbol"]] = allocated_by_base.get(opp["base_symbol"], 0.0) + recommended
        else:
            opp["risk_flags"].append("資金配分上限に達したため今回は推奨サイズ$0(次点候補)")

        del opp["_provisional_score"]

    opportunities.sort(key=lambda o: o["composite_score"], reverse=True)
    for i, opp in enumerate(opportunities, start=1):
        opp["rank"] = i
    return opportunities


def score_and_rank(
    carry_rows: list[dict],
    spread_rows: list[dict],
    capital_usd: float = TOTAL_CAPITAL_USD,
    intel_exchanges: set[str] | None = None,
) -> list[dict]:
    if intel_exchanges is None:
        intel_exchanges = load_intel_exchange_flags()

    normalized = []
    for row in carry_rows:
        opp = normalize_carry_row(row)
        if opp:
            normalized.append(opp)
    for row in spread_rows:
        opp = normalize_spread_row(row)
        if opp:
            normalized.append(opp)

    for opp in normalized:
        score_opportunity(opp, intel_exchanges)

    return build_portfolio(normalized, capital_usd)


# ---------------------------------------------------------------------------
# CLI (単独実行・デバッグ用): 既存スキャナーが出力したCSVを読み込む
# ---------------------------------------------------------------------------


def _read_csv(path: str) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        print(f"警告: {path} が見つかりません(先に対応するスキャナーを実行してください)", file=sys.stderr)
        return []


def write_csv(rows: list[dict], path: str) -> None:
    fieldnames = [
        "rank",
        "opportunity_type",
        "label",
        "exchanges",
        "base_symbol",
        "apr_pct",
        "verified",
        "volume_usd",
        "recommended_position_usd",
        "est_annual_profit_usd",
        "risk_multiplier",
        "composite_score",
        "risk_flags",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            row_out = {k: r.get(k) for k in fieldnames}
            row_out["exchanges"] = ",".join(r.get("exchanges", []))
            row_out["risk_flags"] = " / ".join(r.get("risk_flags", []))
            writer.writerow(row_out)


def main():
    parser = argparse.ArgumentParser(
        description="carry/spread候補を横断してリスク調整後にランキングし、推奨ポジションサイズを付与する"
    )
    parser.add_argument("--carry-csv", default=CARRY_CSV_DEFAULT)
    parser.add_argument("--spread-csv", default=SPREAD_CSV_DEFAULT)
    parser.add_argument("-o", "--output", default=OUTPUT_CSV_DEFAULT)
    parser.add_argument("--capital-usd", type=float, default=TOTAL_CAPITAL_USD)
    args = parser.parse_args()

    carry_rows = _read_csv(args.carry_csv)
    spread_rows = _read_csv(args.spread_csv)

    ranked = score_and_rank(carry_rows, spread_rows, args.capital_usd)
    write_csv(ranked, args.output)

    print(f"{len(ranked)} 件を評価しランキング -> {args.output}", file=sys.stderr)
    for opp in ranked[:TOP_N_RECOMMENDED]:
        print(
            f"  #{opp['rank']} {opp['label']} | APR {opp['apr_pct']:.2f}% | "
            f"推奨${opp['recommended_position_usd']:,.0f} | 年換算想定利益${opp['est_annual_profit_usd']:,.0f}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
