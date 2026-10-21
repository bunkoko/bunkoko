# このリポジトリで作業する Claude へ

- 利用者への説明は日本語で、専門用語を避けてやさしく書く
- 実行は `./cfd <コマンド>`（一覧は `./cfd help`）。Python は `.venv/bin/python`
- `data/`・`output/` はコミットしない（.gitignore 済み。証券会社のデータ・ニュースの見出しを含む）。
  講座のノートや資料の中身もコミットしない（このリポジトリは公開）
- 研究の仮説と判定の条件は、実行する前に `docs/*.md` に書く（事前登録）。結果を見てから条件を変えない
- EA の照らし合わせ（`./cfd demo-check`・`./cfd replay`）を直したら、`.venv/bin/python -m pytest -q tests/test_demo_check.py tests/test_demo_check_phillip.py`
  で確かめる（フィリップと同じ形の作ったデータで毎週の確認を通しで流す。数秒）。点検のために作業員（サブエージェント）を
  何体も出すのは、利用者が頼んだときだけ
- Mac で記録（`git commit`）したら、その場で `git push` する（Mac の中だけの記録があると、`./cfd update` が
  クラウドの更新を取り込めずに止まる。2026-10-14 に 5 つの記録がそうなり、`claude/mac-local` に送って合わせた）

## ニュースの取得・判定（`./cfd news`）が止まったとき

詳しくは `docs/news.md`。

1. 状態を見る: `./cfd news status`（まとめての実行がどこで止まったか・理由・集めた量・空き容量）
2. 記録を見る: `tail -50 data/news/run.log`
3. よくある理由:
   - 空き容量が足りない → `./cfd news clean`（比べるためだけに取ったモデルと一時置き場を消す。データは消さない）。
     それでも 5GB に届かなければ、何を消すかは利用者に相談する
   - GDELT に断られた・回線の不調 → 時間をおいて再開する。「GDELT の API は休み中」は、断られた後に 1 時間
     問い合わせを控えているだけ（生データの取得は続く）
   - 部品が無い → `./cfd update && ./cfd news setup`
4. 再開: `nohup ./cfd news run >/dev/null 2>&1 &`
   - 何度実行してもよい（取得済みの所は飛ばす）。止まっても 10・30・60 分待って自動でやり直す
   - 全部で 8〜9 時間かかるので裏で動かし、終わるのを待たずに利用者へ伝える。様子は 1・2 で見る
5. 判定に使う埋め込みのモデル（`data/news/model_choice.json`）は決定済み。変えない（`compare --force` もしない）
6. 終わったら（status が「完了」）、`output/news/<日時>/report.md` の中身を利用者に見せる

## スマホ（遠隔操作）から頼まれたとき

利用者は外出先から短い言葉で頼むことが多い。次のように受け取る。

| 頼まれ方の例 | やること |
|---|---|
| 「ニュースの状況は？」 | 上の 1・2 を見て、どこまで進んだか・あと何時間かを短く伝える |
| 「結果を見せて」「やさしく説明して」 | 最新の `output/news/<日時>/report.md` を読み、仮説ごとに「使えそう／使えない／まだ判断できない」と理由を伝える |
| 「足りない分を取り直して」 | status が「完了（一部まだ取れていない）」なら、上の 4 で再開する（取れた所は飛ばす） |
| 「判定だけやり直して」 | `./cfd news report`（数分〜数十分。見出しは取り直さない） |
| 「毎日の自動取得は動いてる？」 | `./cfd news status` の自動実行（launchd）の行と、集めた日の最新日付を見る |
| 「空き容量は？」「片付けて」 | `df -h ~` と `./cfd news clean --dry-run` で見せてから `./cfd news clean` |
| 「最新にして」 | `./cfd update`。ただし status が「実行中」「待ち」の間はしない（終わってから） |
| 「EA は動いてる？」 | `./cfd where` のデータフォルダの `MQL5/Logs/<日付>.log`（UTF-16）に、毎日 `足を判断` の行が出ているか |
| 「トレンドの改良を確かめて」 | `./cfd trend`（数分。期間B が加わる）。結果の `output/trend/<日時>/report.md` を見せ、`docs/trend.md` 5 章に記録を足す |
| 「短い時間足を確かめて」 | `./cfd intraday --source mt5`（フィリップの M5 が `data/` に要る。無ければ Mac の前で CfdExportBars） |
| 「暗号資産の記録は動いてる？」 | `./cfd crypto paper --report`。毎朝 6:30 の登録は `launchctl print gui/$(id -u)/com.bunkoko.crypto` |
| 「他の通貨の記録は？」「C3 を確かめて」 | 記録は毎朝の `crypto paper`（と `crypto live`）が BTC・ETH の後に `data/crypto/paper/log_extra.csv` に書く。様子は `./cfd crypto paper --report` の「他の通貨」の節（GMO の一覧に無くて飛ばした記号も出る）。過去の検証は Mac で `./cfd crypto extra --fetch`（数分。クラウドからは Yahoo に断られる）。`output/crypto/extra/<日時>/report.md` の 1 章を見せ、`docs/crypto.md` 9.5 に記録する。条件（9.2・9.3）と通貨の組は変えない。少額の本番に足すのは 9.2 の条件を満たし、利用者がはっきり頼んだときだけ |
| 「暗号資産の本番は動いてる？」「止めて」 | `tail -20 data/crypto/live/log.csv`。止めるのは `touch data/crypto/live/STOP`（再開は消す）。`--real` への切り替えと `config.json` の金額は、利用者がはっきり頼んだときだけ変える（`docs/crypto.md` 8 章） |
| 「キャリーを確かめて」 | `./cfd carry --fetch`（EIA から WTI・天然ガスの限月を取って計算。数分）。`output/carry/<日時>/report.md` を見せ、`docs/carry.md` 6 章に記録を足す |
| 「キャリーの絞り込みを確かめて」 | `./cfd carry --gate`（タートルに絞り込みを足す 3 期間の検証。数分）。`output/carry/<日時>/gate.md` を見せ、`docs/carry.md` 7 章に記録を足す |
| 「大型株をルールで選ぶ検証を確かめて」 | `./cfd stockpick --fetch`（Yahoo から約 240 銘柄。数分）。`output/stockpick/<日時>/report.md` を見せる。1 銘柄の予測は頼まれてもしない（`docs/stockpick.md` 1 章） |
| 「株の研究を確かめて」「上がりそうな株は？」 | `./cfd stocks`（数分。データを新しくするなら `--fetch` で 20〜40 分）。`output/stocks/<日時>/report.md` の 1 章（判定）と 4 章（今あてはまるもの）を見せる。「使えそう」以外は勧めない。結果は `docs/stocks.md` 10 章に足す |
| 「株の短期の売買を確かめて」 | `./cfd shortterm`（10〜20 分。`./cfd stocks --fetch` の後）。`output/shortterm/<日時>/report.md` の 1 章を見せ、`docs/stocks_short.md` 11 章に足す |
| 「5 つの AI の様子は？」「IR の評価を見せて」 | `./cfd agents status`。採点は最新の `output/agents/<日時>/report.md`（1 章の判定・4 章の台帳）。動かし直すなら `./cfd agents run`（10〜20 分。値段も新しくするなら `--prices`）。結果は `docs/agents.md` 9 章に足す |
| 「台帳は動いてる？」「M2 の先のデータは？」 | `./cfd agents check`（数秒）。`output/agents/check/<日時>.md` の 1 章（自動実行）・2 章（抜けた営業日）・3 章（途中経過と判定）を伝える。判定は 250・500・750 日の時点と毎週の見張りの警報だけで、途中の数字を見て何も変えない（`docs/agents.md` 12 章）。初めて動かしたら、3 章の「週の差の標準偏差」を 12.6 に書き写す。でたらめ・E1 の帳は `data/agents/ledger_extra.csv`（`ledger.csv` は今までのまま。調査部が読むので書き方を変えない） |
| 「やることは？」「次は何をすればいい？」 | `docs/todo.md`（利用者が Mac の前や登録でやること）の、済んでいない行を上から伝える。取りかかると言われたら、その行の「やり方」を 1 手ずつ案内する。済んだら「済み」に日付を書く |
| 「J-Quants を確かめて」 | 初回は利用者に J-Quants の無料の登録と API キーの発行（ダッシュボードの設定 → API Key）を頼み、Mac の前で `./cfd jquants key`（キーを貼る。画面には出ない）。その後 `./cfd jquants check`（数分〜20 分）。`output/jquants/<日時>/check.md` の 2 章を見せ、`docs/survivorship.md` 7 章に記録する。有料の契約は利用者がはっきり頼んだときだけ。取ったデータはコミットしない |
| 「EDINET を確かめて」「文章の信号を確かめて」 | 初回は利用者に EDINET の登録と API キーの発行を頼み（`docs/todo.md` の 7）、Mac の前で `./cfd edinet key`。その後 `./cfd edinet check`（数分）の `output/edinet/<日時>/check.md` を見せる。2016-10 ごろまで取れて節が取れていれば、`mkdir -p data/edinet` の後に `nohup ./cfd edinet fetch >> data/edinet/fetch.log 2>&1 &`（5〜6 時間。止めても続きから。取得の間は Mac を眠らせない（ふたは開けて電源につなぐ）。「状態 -1」は回線が切れたこと（Mac が眠った・Wi-Fi）で、同じコマンドで続きから。一覧は 2 秒・書類は 3 秒に 1 件、同時に 1 本。速くしない）→ `./cfd edinet momentum`（1C）・`./cfd edinet lazy`（案 2）。`output/edinet/<日時>/` の 1 章を見せ、`docs/text_signals.md` 12 章に記録する。条件（3・4 章）は変えない。取った本文はコミットしない |
| 「夜だけ持つ記録は？」「J2 の様子は？」 | `./cfd overnight --report`（数秒。1306 の日足を Yahoo から取り直す）。`output/overnight/<日時>/report.md` の 1 章を伝える。判定は 250・500・750 夜の時点だけで、途中の数字を見て何も変えない（`docs/overnight.md`）。届いたら 7 章に書き写す。本物で始めたいと言われたら、6 章（別の現金で引けの買い・まず 1 単位で 3 日試す）を先に案内する |
| 「今週の手で買う候補は？」「M2 を手で買うなら？」 | `./cfd agents manual`（数秒）。出た 10 銘柄と売る株をそのまま伝え、「買うのは決めた次の営業日、S 株なら 10:30〜13:50 に成行（その日の引け）。M2 は検証中で勧めではない」を添える。週の最初の営業日の 13:05 の自動実行が `ledger_extra.csv` に帳 `m2_manual` を書く（`docs/agents.md` 13 章） |
| 「手で買う形の検証をして」 | `./cfd agents manual --test`（数分）。`output/agents/<日時>/manual.md` の 1 章（M2a・M2b）を見せ、`docs/agents.md` 13.5 に記録する。条件（13.2）は変えない。過去の検証は M2a・M2b とも「まだ判断できない」（2026-10-20）。先の記録（帳 `m2_manual`）の判定の条件は 13.6 に書いた（変えない。52・104・156 週の時点だけ） |
| 「キャリーの記録は？」 | `./cfd carry --paper`（1〜2 分。Yahoo の限月ごとの WTI を 1 秒に 1 件）。月に 1 回でよい。`output/carry/paper/<日時>/report.md` の 1・2 章を伝える。判定は 12・24・36 か月の時点だけ（`docs/carry.md` 8 章）。届いたら 8.4 に書き写す |
| 「決算をまたぐ検証をして」「J7 の様子は？」「J7 の点検を動かして」 | 過去の検証は `./cfd agents premium --test`（数分。C7 で見た Yahoo の決算日より前の決算だけ。1 章が判定、2 章が点検）。先の記録は `./cfd agents premium`（数秒）。`output/agents/<日時>/premium.md` を見せ、`docs/earnings_premium.md` 6 章（判定）・7.4（点検。読み方は 7.2、決めることは 7.3 の通り）に記録する。条件（3・4・7.2・7.3 章）は変えない。過去の検証は「使えそう」（2026-10-20）だが、点検（7.4）で大当たり頼みの印が出たので、少額の本番は勧めない（4 章の 250 営業日の先の記録と J-Quants での確かめ直しを待つ。チームの見当はコストの後でほぼ 0、1 年では「まだ分からない」の見込みが高い）。点検は取り直さない。J7b は登録しないと決めた |
| 「geo_vol を他の市場で確かめて」 | `./cfd news volcheck`（数分。S&P500・TOPIX・BTC。値段は Yahoo）。`output/news/<日時>/volcheck.md` の 1 章を見せ、`docs/text_signals.md` 12 章に記録する。判定（5 章）は変えない。通っても EA・暗号資産の量はすぐには変えない（2027-10 の先の確かめの後に別に登録） |
| 「相方の行き過ぎを確かめて」 | `./cfd partner`（数十分。無い市場は Yahoo から取る）。`output/partner/<日時>/report.md` の 1 章を見せ、`docs/cross_asset.md` 7.6 に記録する。判定の条件（7.3）は変えない。採用候補でも EA にはすぐ入れない（デモの区切りで 1 回に 1 つ） |
| 「今日の株の注文は？」 | `./cfd agents orders`（日本株の予行演習。注文は出さない。平日 13:05 の自動実行が先に作って知らせていれば、それを出す）。出た一覧（売る株・買う株・株数・締めまでの時間）をそのまま伝え、「13:30 までを目安に（遅くとも 13:50）、預り区分は必ず特定、送信の前に銘柄の番号・買いか売りか・株数を見比べる」を添える。約定を伝えられたら `./cfd agents orders --fill <銘柄> <買い/売り> <株数> <値段>`（`--status 不成立`・`--status 受注停止`・朝の売りは `--at 寄り`）。判断がまだ無い日に売りだけ要るなら `--sells-only`。利用者の手順は `docs/stocks_live_daily.md`、決まりは `docs/stocks_live.md` 4 章 |
| 「知らせが来ない」「知らせを設定して」 | 設定は `~/.bunkoko/notify.json`（リポジトリの外。権限 600。無ければ何も送らない）。`docs/stocks_live_daily.md` 5 章の順（メッセージ → メール → Pushover）で Mac の前で案内し、`./cfd agents orders --notify-test` で試す。来なかった日は `tail -5 data/agents/live/notify.log` と run.log の「予行演習」の行を見る。もう一度送るなら `./cfd agents orders --notify` |
| 「株の予行演習を止めて」 | `touch data/agents/live/STOP`（新しく買わない。持っている株は予定の日に売る）。再開（STOP を消す）は利用者がはっきり頼んだときだけで、理由を `docs/stocks_live.md` 10 章に書く。金額（`--setup --equity`）と本物への切り替え（`--setup --real`）も、利用者がはっきり頼んだときだけ |
| 「予行演習の様子は？」 | `./cfd agents orders --report`（数秒〜1 分。基準の値段を Yahoo から取る）。`output/agents/live/<日時>.md` の 1 章（止める決まり）・3 章（空運転の進み具合）・4 章（c* の途中経過）を伝える。途中の数字を見て決まりを変えない。c* が決まったら `docs/stocks_live.md` 10 章に書き写す |
| 「儲かりそうなルールは？」「ルールの一覧を見せて」 | `docs/rules_menu.md`（信頼性が低いものも含めた一覧。★の見込み・私たちの数字・実行のしやすさ）。利用者が選んだら、金額の上限・止める決まり・記録のしかたを先に決めて少額の本番で始める（勧めるのは利用者が選んだものだけ。1 銘柄の予測はしない） |
| 「お金の置き場所は？」「NISA はどうする？」 | `docs/portfolio.md` の 1・5・6 章を短く伝える。決めるのは利用者。チームの勧めは案 A。売買の設定は変えない |
| 「開示の収集は動いてる？」 | `./cfd agents status`（本文・決算の数字の件数）と `tail -20 data/agents/collect.log`。登録は `launchctl print gui/$(id -u)/com.bunkoko.agents.collect`。過去の数字の取得の様子は `tail -3 data/agents/xbrl_history.log`（止まっていたら `nohup ./cfd agents collect --xbrl-history --max-docs 40000 --no-embed > data/agents/xbrl_history.log 2>&1 &` で続きから） |
| 「決算の数字の検証をして」 | 過去の数字の取得が終わっていれば `./cfd agents earnings`（数分〜十数分）。`output/agents/<日時>/earnings.md` の 1 章を見せ、`docs/agents.md` 10.4 に記録を足す。条件（10.2）は変えない |
| 「足した項目の検証をして」 | `./cfd agents extra`（20〜30 分。出来高を取り直すなら `--fetch`、学び直さないなら `--reuse`）。`output/agents/<日時>/extra.md` の 1 章を見せ、`docs/agents.md` 11.7 に記録を足す。条件（11.5）は変えない |
| 「調査部を動かして」「調査部の成績は？」 | AI の調査部（証券会社・運用会社の手順にならった分業）は、株用のフォルダ `/Users/bunkoko/dev/kabu` に移した（2026-10-14）。そこの `CLAUDE.md` と `docs/desk.md` に従う。このリポジトリの値段・開示・点数は、そこから読まれている（消さない・場所を変えない） |
| 「毎週の確認をして」 | 書き出し済みなら `./cfd data` → `./cfd replay --start … --equity …`（`--count-from` は付けない）→ `./cfd demo-check`（`docs/operations.md` 12 章の「今の照らし合わせの指定」をすべて付ける。今は `--start 2026-10-07 --equity 10000000`）。`output/demo_check/<日時>/report.md` の「一致／差あり／まだ比べられない」と本番に進む条件の数を伝える。書き出しがまだなら、Mac の前でしてもらう |
| 「本番に進んでいい？」 | 最新の demo-check の report.md と `docs/operations.md` 12 章「本番に進む条件」（事前登録）を照らす。口座を替えた後は、前の口座の report.md と期間・取引を足す。条件は変えない |
| 「EA の照らし合わせで差が出た」 | report.md の「差の理由の候補」を伝える。直したら `--count-from <直した日>` を足して確かめ直し、12 章の「今の照らし合わせの指定」も書き換える |

- 画面の操作が要ることはできない: MT5 のスクリプト（`CfdExportBars`）・EA の貼り付けや設定・MetaEditor のコンパイル・
  各種ログイン。「Mac の前でお願いします」と伝え、手順は `docs/operations.md` から短く示す
- 売買の設定（`./cfd preset`・`./cfd risk`）や MT5 に置くファイルは、利用者がはっきり頼んだときだけ変える
- 適時開示の書類は、TDnet の公開サイト（release.tdnet.info）からは自動で取らない（robots.txt で断っている）。東証の上場会社情報サービスから
  取るときは、**1 秒に 1 件**（`cfdbot/agents/collector.py` の INTERVAL。利用者が 2026-10-13 に決めた速さ）で、同時に 1 本だけ。
  これより速くしたり並列にしたりしない
