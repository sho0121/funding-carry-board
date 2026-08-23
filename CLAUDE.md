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
| 「エアドロップ情報ある?」「今日のエアドロップ調査結果は?」 | `edge_playbook.md` の「2-2. エアドロップ/ポイント制度ファーミング」セクションを確認(毎日JST8:00にクラウドルーティン`daily-airdrop-edge-research`がWebSearchで自動更新している。実行履歴は claude.ai/code/routines で確認可能、このセッションからは直接見えない) |
| 「今どの取引所のどの銘柄のfundingが高い?」「取引所ごとのランキング見せて」 | `exchange_funding_ranking.json`(無ければ `python3 exchange_funding_ranking.py` で更新)を確認。**これは裁定ペアのランキングではなく単一銘柄のfundingをそのまま並べたもの**である点をrisk_manager由来のランキングと混同しないこと |
| 「ダッシュボード更新して」 | `python3 generate_dashboard.py` を実行(carry/spread/ranking/intel/team/paperbot/edge/exchangerankingの8データを再取得しHTMLに埋め込む。edgeX追加とInjective全件検証化により数分〜十数分かかる場合がある) |

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
- **Injectiveのデータは要注意**: Injectiveは誰でも無許可でspot/derivative市場を
  作成できるため、データ取得元のIndexer APIはHelix(公式フロントエンド)が採用して
  いない市場も"active"として返すことがある(実例: AR/USDC PERPはAPI上activeだが
  Helixの検索には出てこないことを実機検証で確認済み、2026-08-20)。APIに「Helix採用
  済みか」を示す明示的なフラグが無いため、実出来高が閾値($20,000)未満かどうかを
  代理指標として使い、該当行は各スキャナーのexcludedリストに回している。Injective
  関連の数値(特に極端なAPR)をユーザーに伝える際は、この限界を踏まえること。
  2026-08-20時点で `funding_spread_scanner.py` は出来高不明な行を全件検証するよう
  修正済み(以前は上位30件のみで下位にゴースト市場が残っていた)
- **Injective = Helixではない**: Injectiveはブロックチェーン本体(Layer1)、Helixは
  その上に構築された公式・最大手のトレーディングフロントエンド。誰でも無許可で
  Injective上に市場を作れるため、Helix以外の(または非採用の)市場も存在しうる
- **`exchange_funding_ranking.py`(取引所別ランキング)での表示名は"Helix"**:
  Helix専用の公開APIは存在しない(Helixの実データはWebSocket/Worker経由と見られ、
  通常のネットワーク監視では捕捉できないことを実機調査で確認済み)ため、データ取得元は
  引き続きInjectiveのIndexer APIのみ。ただしこのモジュールは絶対値上位40件について
  実出来高($20,000以上)を検証してから表示するため(他取引所より広めの検証範囲)、
  「実出来高で検証済みのInjectiveデータ=実質的にHelixで取引可能な銘柄」という前提で
  表示名を"Helix"としている。プラス・マイナス両方のfundingを絶対値上位15件表示する
- **edgeX/dYdX/ApeX/Raydium追加**: いずれも差分スキャナー(`funding_spread_scanner.py`)
  にのみ追加。spot取引が無いためキャリー側(`multi_exchange_arbitrage.py`)には非対応。
  edgeXは株式/コモディティ連動の合成perp(AAPL/XAU等)も扱っており、仮想通貨限定
  ではない点に注意。edgeX/ApeXはbulk取得APIが無く契約ごとに呼ぶ必要があり取得に
  1分前後かかる。dYdXは1回のAPI呼び出しで全銘柄取得できる(bulk対応)
- **Raydium = Orderly Network白ラベル**: Raydium Perps(perps.raydium.io)は独自の
  板ではなく、Orderly Networkという複数フロントエンド共有CLOB基盤の白ラベル展開。
  Raydium固有の市場一覧を返す公開APIは存在せず(Raydium側の市場データはWebSocket
  配信のみで、通常のネットワーク監視では捕捉できないことを実機調査で確認済み)、
  共有API(`api.orderly.org`)は全フロントエンド分の市場をまとめて返す。ただし
  symbol命名規則が標準形式("PERP_BASE_USDC" 80件)とブローカー専用形式
  ("PERP_BASE_USDC_ブローカー名" 57件、mythos/alpix/fastx等の他フロントエンド
  限定の株式・合成資産銘柄)に綺麗に分かれており、実機のRaydium UIに表示される
  銘柄(ETH/BTC/SOL/HYPE等)が全て標準形式側に一致することを確認済みのため、
  標準形式のみをRaydium向けとして`fetch_raydium_perps()`(funding_spread_scanner.py)
  で扱っている。ブローカー専用形式は除外
- **見送った取引所**: Uniswap(perp非対応)、PancakeSwap Perps(Asterのオーダーブック
  基盤をそのまま使っており重複。2026年時点で改めて確認済み)、Vertex/Drift/RabbitX
  (安定した公開APIエンドポイントを確認できず)、Variational(2026年時点でtrading
  API未公開)、GMX(周期的fundingではなく建玉不均衡ベースの借入手数料方式で既存
  スキーマに不適合)。確実な情報が得られれば再検討する

## AI社員(サブエージェント, `.claude/agents/`)

`research-analyst` / `market-intel-analyst` / `risk-manager` / `ops-reporter` /
`portfolio-manager` / `edge-researcher` の6体。深い調査やリスク解釈が必要な質問は
これらに委任してよいが、簡単な確認(上表)はメインセッションでスクリプトを直接実行して
即答してよい。
