# Funding Carry Board — 仮想通貨アビトラ事業部

Hyperliquid / Aster / Backpack / Injective を対象に、ファンディングレート裁定
(spot+perpキャリー、perp対perp差分)の候補を自動収集し、リスク調整後にランキングする
ダッシュボード。毎時 GitHub Actions で自動更新される。

**現在の運用範囲**: シグナル検出・リスク評価・推奨ポジションサイズの提示まで。
取引所APIキー・自動発注は未設定のため、実際の発注は手動で行う。投資助言ではない。

## データフロー

```
各取引所フェッチャー (hyperliquid/aster/backpack/injective_dual_listed.py, *_funding_arbitrage.py)
        │
        ├─ multi_exchange_arbitrage.py … spot+perpキャリー候補を統合
        └─ funding_spread_scanner.py  … perp対perp差分候補を検出
                │
                ├─ risk_manager.py … 2系統を横断し、リスク・出来高・資金配分制約を
                │                    加味した composite_score でランキング +
                │                    推奨ポジションサイズを算出
                └─ market_intel.py … DeFiLlama(ハッキング等リスクイベント) / CoinGecko
                                     (トレンド銘柄)を認証不要APIから収集
                        │
                        ▼
        generate_dashboard.py → hyperliquid_funding_dashboard.html (4タブ)
                                   🏆 推奨ランキング / ファンディングキャリー /
                                   Perp差分スキャナー / 📰 市場インテリジェンス
                        │
                        └─ daily_report.py → reports/YYYY-MM-DD.md (日次サマリー)
```

`.github/workflows/refresh-dashboard.yml` が毎時 `generate_dashboard.py` を実行し、
更新されたHTMLを自動コミットする。この経路は純Python・LLM呼び出し不可のため、
`risk_manager.py` と `market_intel.py` は認証不要の公開APIのみで完結している。

定性的な調査(新興取引所の是非・個別ニュースの深掘り)はこの自動経路では行えないため、
`market-intel-analyst` サブエージェントがオンデマンドで担当し、結果を
`market_intel_notes.md` に蓄積する。`risk_manager.py` はこのファイルを読み、該当取引所の
リスクスコアに反映する。

## AI社員(サブエージェント)

`.claude/agents/` に4つの役割を定義している。Claude Codeに「〜担当として」と話しかければ
該当エージェントが呼び出される。

| エージェント | 役割 | 主な入出力 |
|---|---|---|
| `research-analyst` | 新規裁定機会・対応取引所拡大の定量調査 | 各スキャナーを実行・要約 |
| `market-intel-analyst` | 新興取引所・ニュースの定性調査 | `market_intel_notes.md` に追記 |
| `risk-manager` | リスク評価・推奨ポジションサイズ・ランキングの解釈 | `risk_manager.py` |
| `ops-reporter` | 日次レポート作成、ダッシュボード/CIの健全性確認 | `daily_report.py` |

## 主要スクリプト

- `<exchange>_dual_listed.py` : 取引所ごとにspot/perp両方上場の銘柄を検出
- `<exchange>_funding_arbitrage.py` : spot+perpキャリー候補(取引所別)
- `multi_exchange_arbitrage.py` : 上記を全取引所分統合
- `funding_spread_scanner.py` : perp対perp差分裁定候補
- `risk_manager.py` : リスク調整後ランキング + 推奨ポジションサイズ
- `market_intel.py` : リスクイベント・トレンド銘柄の自動収集
- `generate_dashboard.py` : 上記すべてを `hyperliquid_funding_dashboard.html` に埋め込み
- `daily_report.py` : 日次Markdownレポート生成

## ローカル実行

```bash
python3 generate_dashboard.py   # ダッシュボードHTMLを最新化
python3 risk_manager.py         # risk_assessed_opportunities.csv を単独生成(デバッグ用)
python3 market_intel.py         # market_intel.json を単独生成(デバッグ用)
python3 daily_report.py         # reports/YYYY-MM-DD.md を生成
```

## ロードマップ

- **Phase 2**: 市場インテリジェンスの定期スケジュール化・情報源拡充
- **Phase 3**: バックテスト / ペーパートレード
- **Phase 4**: 取引所APIの読み取り専用連携(残高・ポジション照会)
- **Phase 5**: 半自動執行(シグナル→人間承認→発注)
- **Phase 6**: 完全自動化(希望する場合のみ)

現時点ではAPIキー未設定のため Phase 4 以降は未着手。
