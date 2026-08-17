#!/usr/bin/env python3
"""
Hyperliquid の Info API から perps と spot の両方に上場している銘柄一覧を取得する。

- type: "meta"      -> perps の universe (ベースシンボル名がそのまま name)
- type: "spotMeta"  -> spot の tokens / universe
    universe の各エントリは {"tokens": [baseIndex, quoteIndex], "name": "BASE/QUOTE", ...}
    tokens[baseIndex] を tokens リストで引くとベーストークンの実名が分かる
    (universe.name の "@123" のような表記はベース名の代わりにならないため使わない)

BTC や SOL のような主要銘柄は、spot 側では "UBTC" / "USOL" のように Hyperliquid の
ブリッジ資産 ("Unit" トークン, fullName が "Unit Bitcoin" 等) として上場されており、
そのままの文字列では perps の "BTC" / "SOL" と一致しない。
そこで単純な完全一致に加え、以下の Hyperliquid 側の命名規則を使った対応関係も探す。

  1. exact           : spot のベースシンボル名 == perps のシンボル名 (例: PURR, TRUMP)
  2. unit_prefix      : fullName が "Unit " で始まるブリッジ資産トークンの名前から
                        先頭の "U" を除去したものが perps のシンボル名と完全一致
                        (例: UBTC -> BTC, USOL -> SOL, UETH -> ETH)
  3. unit_prefix_k    : 2 で一致しない場合、"k" + (U を除去した名前) が perps の
                        シンボル名と完全一致 (ミームコインの 1000 倍建て表記。
                        例: UBONK -> kBONK)
  4. unit_prefix_startswith : 2, 3 でも一致しない場合、perps のシンボル名が
                        (U を除去した名前) から始まる (例: UFART -> FARTCOIN,
                        UVIRT -> VIRTUAL)。誤爆を避けるため 4 文字以上の場合のみ試す。

2〜4 は fullName が "Unit " で始まるトークン (Hyperliquid 公式のブリッジ資産) にのみ
適用し、無関係なトークンが偶然 "U" で始まっていても対象にしない。
"""

import argparse
import csv
import json
import re
import sys
import urllib.request

API_URL = "https://api.hyperliquid.xyz/info"
UNIT_PREFIX_RE = re.compile(r"^U+")


def fetch(request_type: str) -> dict:
    body = json.dumps({"type": request_type}).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_perp_symbols() -> set[str]:
    meta = fetch("meta")
    return {entry["name"] for entry in meta["universe"]}


def get_spot_pairs() -> list[dict]:
    """spot の各ペアについて、ベーストークンの情報を付与したリストを返す"""
    spot_meta = fetch("spotMeta")
    tokens = spot_meta["tokens"]
    token_by_index = {t["index"]: t for t in tokens}

    pairs = []
    for pair in spot_meta["universe"]:
        base_index, quote_index = pair["tokens"]
        base_token = token_by_index[base_index]
        quote_token = token_by_index[quote_index]
        pairs.append(
            {
                "base_symbol": base_token["name"],
                "base_full_name": base_token.get("fullName") or "",
                "quote_symbol": quote_token["name"],
                "spot_pair_name": pair["name"],
                "spot_pair_index": pair["index"],
                "is_canonical": pair.get("isCanonical", False),
            }
        )
    return pairs


def match_perp_symbol(base_symbol: str, base_full_name: str, perp_symbols: set[str]):
    """spot のベーストークンに対応する perps シンボルと、一致方法を返す (無ければ None, None)"""
    # 1. 完全一致
    if base_symbol in perp_symbols:
        return base_symbol, "exact"

    # 2〜4 は Hyperliquid 公式のブリッジ資産 ("Unit" トークン) にのみ適用する
    if not base_full_name.startswith("Unit "):
        return None, None

    stripped = UNIT_PREFIX_RE.sub("", base_symbol)
    if not stripped or stripped == base_symbol:
        return None, None

    # 2. U を除去して完全一致
    if stripped in perp_symbols:
        return stripped, "unit_prefix"

    # 3. k プレフィックス (1000 倍建てのミームコイン表記)
    k_candidate = "k" + stripped
    if k_candidate in perp_symbols:
        return k_candidate, "unit_prefix_k"

    # 4. 前方一致 (例: FART -> FARTCOIN, VIRT -> VIRTUAL)。誤爆防止に4文字以上のみ
    if len(stripped) >= 4:
        candidates = sorted(p for p in perp_symbols if p.startswith(stripped))
        if len(candidates) == 1:
            return candidates[0], "unit_prefix_startswith"

    return None, None


def find_dual_listed() -> tuple[list[dict], list[dict]]:
    perp_symbols = get_perp_symbols()
    spot_pairs = get_spot_pairs()

    matched = []
    unmatched_unit_tokens = []

    for pair in spot_pairs:
        perp_symbol, match_type = match_perp_symbol(
            pair["base_symbol"], pair["base_full_name"], perp_symbols
        )
        if perp_symbol:
            matched.append(
                {
                    "perp_symbol": perp_symbol,
                    "spot_base_symbol": pair["base_symbol"],
                    "spot_quote_symbol": pair["quote_symbol"],
                    "spot_pair_name": pair["spot_pair_name"],
                    "spot_pair_index": pair["spot_pair_index"],
                    "match_type": match_type,
                    "is_canonical": pair["is_canonical"],
                }
            )
        elif pair["base_full_name"].startswith("Unit "):
            unmatched_unit_tokens.append(pair)

    matched.sort(key=lambda r: (r["perp_symbol"], r["spot_pair_index"]))
    return matched, unmatched_unit_tokens


def write_json(rows: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def write_csv(rows: list[dict], path: str) -> None:
    fieldnames = [
        "perp_symbol",
        "spot_base_symbol",
        "spot_quote_symbol",
        "spot_pair_name",
        "spot_pair_index",
        "match_type",
        "is_canonical",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Hyperliquid で spot と perps の両方に上場している銘柄一覧を取得する"
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="出力フォーマット (デフォルト: csv)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="出力先ファイルパス (省略時は dual_listed.<format>)",
    )
    args = parser.parse_args()

    output_path = args.output or f"dual_listed.{args.format}"

    rows, unmatched = find_dual_listed()

    if args.format == "json":
        write_json(rows, output_path)
    else:
        write_csv(rows, output_path)

    print(f"{len(rows)} 件の spot ペアが perps と対応しています -> {output_path}", file=sys.stderr)
    if unmatched:
        names = ", ".join(f"{p['base_symbol']}({p['base_full_name']})" for p in unmatched)
        print(f"注: 対応する perps が見つからなかった Unit トークン: {names}", file=sys.stderr)


if __name__ == "__main__":
    main()
