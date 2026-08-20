# Funding Carry Board — Claude Code 向けガイド

このプロジェクトの全体像は [README.md](README.md) を参照。ここではこのディレクトリで
セッションを開始した Claude が、ユーザーの雑談的な質問に迷わず対応できるようにする。

**目標**: ユーザーはスクリプト名やエージェント名を意識せず、普段の会話で聞くだけで
リサーチ・市場動向・損益を確認できるようにする。以下のマッピングに従って動くこと。

## ユーザーがこう聞いたら

| ユーザーの発言(例) | すること |
|---|---|
| 「今どんな裁定機会がある?」「新しい機会ある?」 | `funding_spread_scanner.py` / `multi_exchange_arbitrage.py` を実行し、上位候補を要約。詳しい定量分析は `research-analyst` エージェントに委任してもよい |
| 「今一番いいのはどれ?」「どれ取るべき?」 | `risk_manager.py`(または `generate_dashboard.py` 実行後の `hyperliquid_funding_dashboard.html` の推奨ランキングタブ)を見て、リスク調整後の上位候補を根拠付きで回答 |
| 「市場に何か動きある?」「取引所で何かあった?」「新商品出た?」 | まず `market_intel.json`(直近の自動収集結果。無ければ `python3 market_intel.py` で更新)を確認。より踏み込んだ調査が要る場合は `market-intel-analyst` エージェントに委任し、結果を `market_intel_notes.md` に記録 |
| 「今の損益は?」「含み益どう?」「儲かってる?」 | `python3 portfolio.py status`(オープン中)・`summary`(確定+含み合計)を実行して回答 |
| 「このポジション建てた」「◯◯クローズした」 | `portfolio.py open` / `close` で記録する。銘柄・取引所・サイズをユーザーに確認してから実行(勝手に金額を推測しない) |
| 「Botの成績どう?」「ペーパートレードの結果は?」 | `python3 paper_bot.py status`(オープン中)・`summary`(確定+含み+勝率)を実行して回答。**実弾は一切使っていないシミュレーションである旨を必ず明記する** |
| 「新しい儲け方(エッジ)を探して」「他に何かエッジはないか」 | `edge_playbook.md` を確認した上で、新規の発見・分析が必要なら `edge-researcher` エージェントに委任する。既知エッジの実行可否は `risk-manager` に橋渡しする |
| 「このエッジは検証されてる?」「新規上場あった?」 | まず `edge_playbook.md`(検証状況)・`edge_signals.json`(直近の自動検知結果。無ければ `python3 edge_watch.py` で更新)を確認 |
| 「ダッシュボード更新して」 | `python3 generate_dashboard.py` を実行(carry/spread/ranking/intel/team/paperbot/edgeの7データを再取得しHTMLに埋め込む) |

いずれも投資助言ではなく裁定機会・損益の情報提供であることを踏まえた回答をする。
数値は必ず実行結果に基づき、推測で答えない。

## 現状の制約(必ず踏まえること)

- 取引所APIキー・自動発注は未設定。実際の発注はユーザーが手動で行う
- `risk_manager.py` の `TOTAL_CAPITAL_USD` は実際の運用資金(2026-08時点で$15,001)。
  運用がうまくいけば今後増える想定なので、入出金や増資の話が出たらこの値も
  更新してよいか確認し、更新したら `generate_dashboard.py` を再実行してダッシュボードの
  推奨ランキングにも反映させること
- `positions.json`(収益管理台帳、実運用)は実際の運用金額を含む個人情報。**絶対に
  git管理・外部送信しない**(`.gitignore` 済み)。一方 `paper_positions.json`
  (`paper_bot.py`)はシミュレーションのみで実金額を含まないため公開・git管理してよい
  ── この2つを混同しないこと
- `.github/workflows/refresh.yml` が毎時 `generate_dashboard.py` を実行し
  ダッシュボードHTML・`paper_positions.json`・`edge_watch_snapshot.json` を自動コミット
  する。この経路はLLM呼び出し不可の純Pythonのみ(`risk_manager.py` / `market_intel.py` /
  `paper_bot.py` / `edge_watch.py` はこの制約を満たす設計。APIキーも一切使わない)
- `edge_watch_snapshot.json`(新規上場検知の差分比較用スナップショット)は前回実行との
  比較にしか使わず実金額を含まないため、`paper_positions.json` と同様に公開・git管理
  してよい(`.gitignore` の `!edge_watch_snapshot.json` で明示的に除外解除している)
- ファンディング裁定事業部(既存5体)とエッジ・ラボ(`edge-researcher`)は機能的に別部門。
  エッジ・ラボが「実行可能・検証できそう」と判断したエッジは既存の
  risk-manager/portfolio-manager/paper_botのパイプラインに橋渡しする(並行別事業では
  なく、新しいエッジの入り口を既存の実行系につなぐ設計)

## AI社員(サブエージェント, `.claude/agents/`)

`research-analyst` / `market-intel-analyst` / `risk-manager` / `ops-reporter` /
`portfolio-manager` / `edge-researcher` の6体。深い調査やリスク解釈が必要な質問は
これらに委任してよいが、簡単な確認(上表)はメインセッションでスクリプトを直接実行して
即答してよい。
