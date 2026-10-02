# cfdbot — 原油・銀 CFD 向け自動売買アルゴリズム

ボラティリティの高い商品 CFD（WTI・ブレント原油、銀）向けに、
**Mac で研究・最適化（Python）→ Windows の MT5 で発注（EA）** する構成のツール一式。

- アルゴリズムの設計と選定理由: **[docs/algorithms.md](docs/algorithms.md)**（まずこれを読む）
- 証券会社の決定・口座開設・データ取得から本番までの手順: **[docs/roadmap.md](docs/roadmap.md)**
- 複数銘柄の組み合わせと資金配分をデータから学習（H1、iPad も計算に参加可）: **[docs/training.md](docs/training.md)**
- 戦略は差し替え式。`cfdbot/strategies/` に追加するだけで、バックテスト・最適化・テストの対象になる

| 銘柄 | 主力 | 副 |
|---|---|---|
| 原油 | A. ドンチャン・ブレイクアウト（ATR トレンドフォロー） | C. 押し目買い / 戻り売り |
| 銀 | B. スクイーズ・ブレイクアウト | C. 押し目買い / 戻り売り（発展形: E. レジーム切り替え） |

共通: ATR で数量を決定（1回の損失 = 資金の 1%）、建値ストップ + シャンデリア型トレーリング、
EIA/API 在庫統計・週末前の新規停止、合計・グループ別のリスク上限、日次損失・最大DDでの停止。

## 構成

```
cfdbot/                 Python パッケージ（Mac で研究・バックテスト・最適化）
  data.py               1. データ取得: MT5 の CSV 読み込み、合成データ
  strategies/           2. 戦略（ここだけ差し替える）
    donchian.py           A. ドンチャン・ブレイクアウト
    squeeze.py            B. スクイーズ・ブレイクアウト
    pullback.py           C. トレンド押し目/戻り
    reversion.py          D. レンジ逆張り（補助）
    regime.py             E. レジーム切り替え（複合）
  risk.py               3. リスク管理: 数量計算・上限・停止条件
  exits.py                 出口管理: 損切り・建値・トレーリング・時間ストップ
  backtest.py           4. 発注・約定の模擬（Bid/Ask・窓開け・金利込み）
  events.py                EIA/API・指標イベント・週末・サーバー時刻
  walkforward.py           ウォークフォワード最適化（並列）
  train/                   複数銘柄のポートフォリオ学習（配分・分散計算・レポート）
  export.py                EA 用パラメータ（.set / .txt）の書き出し
  notify.py             5. 監視・通知（n8n などの Webhook）
mql5/Experts/CfdCommodityEA.mq5   本番用 EA（Windows の MT5 で 24 時間稼働）
scripts/                demo / backtest / walkforward / compare_signals
config/                 銘柄仕様（フィリップ・サクソ）、イベント CSV の雛形
docs/algorithms.md      アルゴリズム設計書
tests/                  pytest（先読み検査・約定計算・リスク計算など）
```

## Mac でのセットアップ

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                      # テスト
python scripts/demo.py      # 合成データで一通り動かす（成績は無意味）
```

## ポートフォリオ学習（おすすめ）

MT5 から書き出した CSV を `data/` に置いて実行するだけ。銘柄×戦略×時間足×パラメータの組み合わせと
資金配分をウォークフォワードで決め、レポートと EA 用ファイルを `output/train/<日時>/` に出す。
M5 などの細かい足も置くと約定を細かく再現し、H4・D1 などの上位足は売買の向きのフィルタとしても学習できる。

```bash
python scripts/train.py --demo          # まず合成データで動作確認
python scripts/train.py                 # data/ の CSV で学習（設定: config/train.toml）
python scripts/train.py --listen 0.0.0.0   # iPad など他の端末も計算に参加させる
```

詳しくは [docs/training.md](docs/training.md)。以下は戦略を個別に検証する場合の使い方。

## 実データでの使い方

### 1. MT5 からバーを書き出す

フィリップ MT5（デモ口座）で「表示 → 銘柄」（Ctrl+U）のバーのタブから銘柄・H4・期間を選び、書き出す。
タブ区切りの `<DATE> <TIME> <OPEN> ... <SPREAD>` 形式をそのまま読める。

**サーバー時刻の方式を確認すること**（`--server-tz`）。既定の `ny_close` は
「サーバー時刻 = 米東部時間 + 7 時間（冬 GMT+2 / 夏 GMT+3）」。違う場合は `--server-tz 2` のように UTC からの時差か、
`--server-tz Europe/Athens` のようにタイムゾーン名を指定する。

### 2. バックテスト

```bash
# 推奨構成（原油: ドンチャン+押し目、銀: スクイーズ+押し目）
python scripts/backtest.py --csv WTI=data/WTI_H4.csv --csv SILVER=data/XAGUSD_H4.csv \
    --equity 1000000 --fx-csv data/USDJPY_H4.csv --events config/events_example.csv

# 1回の損失 1.5%、レバレッジ上限 1銘柄 1.5 倍・合計 3 倍で試す（0 で上限なし）
python scripts/backtest.py --csv WTI=data/WTI_H4.csv --csv SILVER=data/XAGUSD_H4.csv \
    --risk 0.015 --max-leverage-symbol 1.5 --max-leverage-total 3

# 戦略を指定、コスト 2 倍のストレステスト
python scripts/backtest.py --csv SILVER=data/XAGUSD_H4.csv --strategy squeeze \
    --params '{"box_period": 24}' --exit '{"trail_atr": 3.5}' --spread-mult 2 --slippage-mult 2
```

結果は `output/` に取引一覧・資産曲線・見送り理由・指標（JSON）として保存される。
スプレッドなどの実測値は `config/instruments_phillip.json` を編集して `--instruments` で渡す。

### 3. ウォークフォワード最適化 → EA 用に書き出し

```bash
python scripts/walkforward.py --csv WTI=data/WTI_H4.csv --symbol WTI --strategy donchian \
    --grid '{"entry_period": [30, 40, 55], "exit_period": [10, 20], "exit.trail_atr": [2.5, 3, 3.5]}' \
    --train 24 --test 6 --export
```

`output/ea/` に次の 2 つができる。

- `cfdbot_WTI_donchian.set` … MT5 の EA 設定画面の「読み込み」で使う
- `cfdbot_WTI_donchian.txt` … EA が定期的に読み直すファイル。Windows の
  `%APPDATA%\MetaQuotes\Terminal\Common\Files\` に置き、EA の `ea_param_file` にファイル名を指定

### 4. Windows（MT5）で EA を動かす

1. `mql5/Experts/CfdCommodityEA.mq5` を MT5 の `MQL5\Experts\` にコピーし、MetaEditor でコンパイル
   （**この環境ではコンパイルしていない**。エラーが出たら修正が必要）
2. チャート（H4）に貼り付け。**1 チャート = 1 銘柄 × 1 戦略**。同じ銘柄で別の戦略を動かす場合は
   `ea_magic` を変えて別チャートに貼る（`ea_magic ÷ 1000` が同じ EA 同士でリスクを合算）
3. `rk_cluster_id` を設定（原油 = 1、貴金属 = 2）。`ft_event_tags` は原油 `oil`、銀 `usd_macro`
4. ストラテジーテスターで `ea_log_signals=true` にして実行し、`scripts/compare_signals.py` で
   Python とシグナルが一致するか確認
5. 通知: MT5 のオプション → 通知 で MetaQuotes ID（iPhone の MT5 アプリ）を設定。
   n8n に送る場合は ツール → オプション → エキスパートアドバイザ で Webhook の URL を許可し、`ea_webhook_url` に指定
6. ミニ PC の自動復帰（通電時起動・自動ログイン・MT5 のスタートアップ登録）は開発コンテキスト 4.3 章を参照

## 注意

- 銘柄仕様のスプレッド・スリッページ・金利は **仮置き**。デモ口座で実測して置き換えること
- フィリップの商品 CFD 銘柄数（8 か 5 か）、MT5 上のシンボル名、サーバー時刻の方式は未確認
- `scripts/demo.py` の成績は合成データによるもので、戦略の有効性とは無関係
- 自分の口座で使う分には問題ないが、ツールを他人に販売・レンタルする場合や有料でシグナルを配信する場合は、
  一般に金融商品取引業の登録が必要と考えられる。その方向を検討するときは専門家に確認すること
