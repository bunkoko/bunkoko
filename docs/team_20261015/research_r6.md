# R6 調査（調査役 / 2026-10-15 / WebSearch のみ）

## 1. EDINET API v2 の公式の形式

**読み方**: 公式の文書（金融庁の仕様書 PDF・EDINET の利用規約・FAQ）は WebFetch が使えず、**PDF 本体は開けていません**。下の「公式」は「検索結果にその公式ページの文が引用されていた」という意味で、ラベルは次の 3 つです。
- **公式（要約で確認）** = 公式サイトの文が検索要約に出た（原文は未読）
- **二次** = 個人ブログ・解説記事（古い版 v1 の説明が混ざる）
- **推測／見つからなかった** = 裏が取れていない

公式の仕様書: EDINET API 仕様書（Version 2）2026 年 6 月版 https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf （2024 年 7 月版の写しは https://time2log.com/wp-content/uploads/2024/08/11_EDINET_API%E4%BB%95%E6%A7%98%E6%9B%B8%EF%BC%88version-2%EF%BC%89.pdf ）。v1 は 2024-03-29 で終了、v2 は 2024-04-01 から（公式 FAQ 系の要約）。

### 1-1 認証と登録（電話の認証は版で違う）
| 項目 | 内容 | 信頼度 |
|---|---|---|
| キーの渡し方 | クエリ文字列の `Subscription-Key=<APIキー>`（書類一覧の例は `…/api/v2/documents.json?date=…&type=2&Subscription-Key=…`）。ヘッダー渡し（Ocp-Apim-Subscription-Key）の記述は**見つからなかった** | 公式（要約）＋二次 |
| 基点 URL | `https://api.edinet-fsa.go.jp/api/v2/` | 二次 |
| 登録の流れ | アカウント作成（サインアップ画面でメールアドレス入力・画像認証）→ **多要素認証** → 「API キー発行画面」→ 連絡先を登録するとキーが表示される。キーは初回に発行したものを続けて使える | 公式（要約） |
| 電話（SMS）の認証 | **2024 年 7 月版の仕様書は「SMS で受け取った確認コードを入力」**。**2026 年 6 月版は「メールアドレスで確認コードを受け取る」書き方**に見える（検索の要約）。つまり**電話が要るかは版で変わった可能性があり、最終確認は 2026 年 6 月版の本文**。個人のブログ（2025-08）は電話番号の認証と書く | 公式（要約・版が食い違う） |
| つまずき | ブラウザがポップアップをブロックするとキー表示画面に進めないことがある。`api.edinet-fsa.go.jp` を許可しておく | 二次 |
| キーの扱い | 画面に出たキーは必ず保存。ソースに直書きせず環境変数に | 二次（一般論） |

→ 利用者に頼むこと: EDINET のアカウント作成と API キー発行（画面操作・多要素認証は Mac の前で。電話の要否は画面で分かる）。キーは `~/.bunkoko/` 側に置き、リポジトリには入れない（J-Quants キーと同じ扱い）。

### 1-2 書類一覧 API（入口は日付ごと）
| 項目 | 内容 | 信頼度 |
|---|---|---|
| URL | `GET /api/v2/documents.json` | 二次 |
| パラメータ | `date`（YYYY-MM-DD、**1 日単位。期間の一括指定はできない**）、`type`（1 = メタデータのみ／2 = 提出書類一覧＋メタデータ。**一覧が欲しいなら 2**）、`Subscription-Key` | 公式（要約）＋二次 |
| 返り値の形 | JSON。`metadata`（件数・状態）と `results` の配列（書類ごと）。成功時 `status` は 200。エラーの表は仕様書 3-3「ステータスコード」（本文は**未確認**） | 公式（要約）＋二次 |
| 主な項目名 | `docID`（書類管理番号。取得 API の入口）、`edinetCode`（提出者）、`secCode`（証券コード **5 桁**。無い提出者は `null`）、`filerName`、`docTypeCode`、`docDescription`、`ordinanceCode`（府令コード、例 030）、`formCode`（様式コード、例 10A000）、`submitDateTime`（例 "2023-06-12 09:00"）、`periodStart` `periodEnd`、`parentDocID`、`withdrawalStatus`（取下区分）、`docInfoEditStatus`（書類情報修正区分）、`xbrlFlag` `pdfFlag` `csvFlag`（"1" のとき CSV 取得可）など | 二次（項目名の綴りは仕様書で照合のこと） |
| 日付で何が引けるか | 引くのは**提出日**。決算期・銘柄では絞れない（`secCode` で絞れるのは取ってきた結果の側） | 推測（仕様から） |

### 1-3 書類の種類コード（有価証券報告書・訂正）
| docTypeCode | 書類 | 信頼度 |
|---|---|---|
| **120** | 有価証券報告書 | 二次（複数の解説で一致） |
| **130** | 訂正有価証券報告書 | 二次（一覧が 120 と 130 をまとめて抜く例あり）。ただし今回の別検索では 130 の表記を**確認できなかった**ので仕様書で照合 |
| 140 | 四半期報告書（廃止後は過去分のみ） | 二次 |
| 160 | 半期報告書 | 二次 |
| 040 | 訂正有価証券届出書（これは別物） | 二次 |

- 訂正の扱い: 訂正書類は**別の docID を持ち、`parentDocID` が元の書類を指す**（訂正届出書の例で確認。有報の訂正も同様と見るのが自然だが、**そこまでは確認できなかった**＝推測）。`withdrawalStatus`（取下区分）と `docInfoEditStatus`（書類情報修正区分）の値の意味は**見つからなかった**。
- 取り込みの決まり案（分析役向け・推測）: ①まず `docTypeCode == '120'` だけで作る。②130 は「その年の有報の本文が変わったか」を見るときだけ別枠。③同じ会社・同じ決算期に複数あれば**最初に提出されたもの**を使う（後から訂正された本文でテストすると、発表時点の情報になっていない）。④`withdrawalStatus` が取下げなら除外。

### 1-4 書類取得 API と本文の取り出し
| 項目 | 内容 | 信頼度 |
|---|---|---|
| URL | `GET /api/v2/documents/{docID}?type=N&Subscription-Key=…` | 公式（要約） |
| type=1 | XBRL 一式の zip（提出本文書・監査報告書）。zip の中に XBRL と、本文の HTML が入る（本文の入り方は**推測**） | 公式（要約）＋二次 |
| type=2 | PDF | 公式（要約） |
| type=3 | 添付文書 | 公式（要約） |
| type=4 | 英訳（一部の書類のみ） | 公式（要約） |
| **type=5** | **XBRL を CSV にした zip**（中に `XBRL_TO_CSV/` のフォルダ）。**UTF-16・タブ区切り**。列は要素 ID・項目名・コンテキスト ID・相対年度・連結個別・期間時点・ユニット ID・単位・値。**書類一覧の `csvFlag` が "1" のものだけ** | 公式（要約）＋二次 |
| 本文を取り出せる要素（type=5 の「要素ID」列、または type=1 の XBRL の要素名） | 有報の本文はテキストブロック要素で、**文章は HTML 断片の形で値に入る**（タグを外して使う） | 二次（複数の解説記事が一致。公式タクソノミ文書では**未確認**） |
| 　事業の内容 | `jpcrp_cor:DescriptionOfBusinessTextBlock` | 二次 |
| 　事業等のリスク | `jpcrp_cor:BusinessRisksTextBlock` | 二次 |
| 　経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析 | `jpcrp_cor:ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock` | 二次 |
| コンテキスト | 解説では `FilingDateInstant` を使う例が多い（本文ブロックが載るコンテキスト） | 二次 |

- **古い年度の注意（重要）**: XBRL 提出は 2008-04-01 以後に始まる事業年度の**財務諸表**から。財務諸表以外（有報の本文）が XBRL 対象に入ったのは **2013-09 の対象拡大（2013 年版タクソノミ、運用 2013-09-17）以降**と読める（金融庁タクソノミ概要の要約から）。よって **2013 年より前の年度は、本文のテキストブロックが無く、PDF・HTML でしか取れない可能性**（推測。年度ごとに要素の有無を実データで確かめる）。
- 確かめ方（無料・1 件）: 任意の有報 1 件を type=5 で取り、「要素ID」列に上の 3 要素があるか、値に文章が入っているかを見る。年度を 2014・2018・2022 と 3 点で。

### 1-5 何年分さかのぼれるか（結論: 現在は **10 年**）
- **公式の根拠 3 つ（検索要約で確認）**:
  1. EDINET 公式 FAQ ページの文: 書類ごとに閲覧期間が定められており、**有価証券報告書は「受理した日から 10 年を経過する日まで」**（https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0090_001.html の要約）。
  2. 関東財務局の公的ページ: 表では公衆縦覧 5 年、本文では「有価証券報告書などは受理日から **10 年**を経過する日まで閲覧期間を延長している」（https://lfb.mof.go.jp/kantou/disclo/gaiyou.htm）。
  3. 財務省系の資料（OGB）: 有価証券報告書・四半期報告書・2024-04-01 以後受理の半期報告書は受理日から 10 年（https://www.ogb.go.jp/-/media/Files/OGB/Zaimu/okizaimu/kigyo/20147kaitei/11kigyou.pdf）。
- **古い記述（5 年）は旧情報**: 2021 年の報道で「5 年を 10 年に延ばす方針」（https://www.sn-hoki.co.jp/article/tamasters/tamaster1689360/）、民間解説は「2023 年のシステム更改で 10 年」。大学図書館の案内には 5 年が残る。v1 時代のブログの「過去 5 年」は v1 の記述。
- つまり**今日（2026-10-15）の時点では、2016-10 ごろ以降に受理された有報までが取れるはず**（推測。`受理日 + 10 年`の計算）。取れる最古の日は、書類一覧を日付で遡って**実際に空になる日**を確かめる（1 日 1 回なので、年に 1 回ずつ見て二分探索すれば数十回）。
- **含意**: 2016 年以降の有報だと、**本文が XBRL に入っている範囲（2013 年以降）に収まる**。TOPIX500 × 約 10 年 ≒ 5,000 社年。前年との比較は最初の年が使えないので実質 9 年分。

### 1-6 回数の決まり・規約（自動の取得・連続の取得）
| 項目 | 内容 | 信頼度 |
|---|---|---|
| 規約の文 | EDINET 利用規約（https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html）に、**API 機能の利用者は「短時間における大量のアクセスその他の API 機能の運用に支障を与える行為」をしてはならない**。該当する（おそれ含む）と判断した場合、**予告なく API の利用を停止**できる。負荷によりアクセス制限をかけることがある。EDINET の FAQ にも同趣旨 | 公式（要約） |
| 数字の上限 | **公式の「1 秒に何回」は見つからなかった**。「1 秒に 1 回」「3〜5 秒に 1 回」という数字は個人ブログの慣例で、公式ではない。429（回数超過）を返すことはあるが、固定の時間枠ではなく、公表もされていない（二次） | 公式にはなし |
| 自動取得そのもの | 禁止ではない（API が自動取得のための仕組み）。禁止は「大量・短時間」 | 公式（要約） |
| 私たちの運用案（推測） | 1 秒に 1 件・同時 1 本（適時開示と同じ決まり）より**さらに遅く、書類一覧は 2 秒に 1 件、書類取得は 3 秒に 1 件**から。429・接続切断が出たら 10・30・60 分待って再開（ニュース取得と同じ流儀）。過去 10 年の有報（TOPIX500 で約 5,000 件）なら、取得 3 秒間隔で約 4 時間、一覧（約 2,500 営業日）が約 1.5 時間。夜間に裏で動かせば足りる | 推測 |
| 再配布 | 取ったデータはコミットしない（公開リポジトリ）。EDINET 自体の書類は公開情報だが、まとめて再配布する話は規約を別に確認 | 推測 |

### 1-7 分析役へ（プログラムに使うときの要約）
1. キー: `Subscription-Key` をクエリで。`~/.bunkoko/` から読む。
2. 一覧: 日付を 1 日ずつ回す。`type=2`。`docTypeCode in {120}` かつ `secCode` が TOPIX500 の 5 桁コード（4 桁コード + `0`）に合うものを残す。
3. 取得: `type=5`（CSV）が第一候補（UTF-16・タブ・要素ID で 3 つの本文ブロックを拾う）。`csvFlag != "1"` なら `type=1` の zip から本文を取る。
4. 前年との比べ方: 同じ `secCode` の翌年を `periodEnd` が約 1 年違うもので組にする。決算期変更の会社は除く。
5. 本文は HTML 断片。タグ除去と空白の正規化を、前年・当年で同じ手順で。
6. 速度: 2〜3 秒に 1 件、429 で長く待つ。再開しても取得済みは飛ばす。
7. 最初の 3 点確認: ①最古の取れる日 ②2014・2018・2022 の 1 件ずつで 3 要素の有無 ③電話の認証が要るか（利用者の画面）。

---

## 2. 決算の発表日のずれ（予定より・前年より遅い会社は悪い知らせが多い）

**ラベル**: 全て二次（検索要約。論文本体は未読）。

### 2-1 見つかった研究
| 研究 | 内容・数字 | 日本・大型・個人 | 弱まりの有無 | 信頼度 |
|---|---|---|---|---|
| Johnson & So (2018) "Time Will Tell"（JFQA 53(6), 2431–2464）| 会社が**事前に公表する発表予定日が、予想より遅い会社は、あとで悪い決算を出す**。市場は発表日までこの情報を織り込まない。「翌週に発表予定の会社を買う」週次の戦略で、4 因子アルファが**週 62〜138bp**（必要な銘柄数による）。予測できるリターンの **60% 超が発表日そのものに集中**。標本は 2006–2013 の米国 18,959 社四半期。従来の「早い/遅い」の区別に上乗せがある | **日本の追試は見つからなかった**。日本でも発表予定日は事前に出る会社が多い（推測、要確認）。ただし大型株の予定日は毎年ほぼ同じで、ずれる会社が少ない（推測）| 2013 年まで。公表後の弱まりは**見つからず** | 二次 |
| 同論文の元の考え（早い発表は良い知らせ、遅い発表は悪い知らせ）| 古くからの結果。Penman は、早い・良い知らせの株を買うと 20 日で平均約 1.0% の超過（要約）。1980〜90 年代の標本では、発表時期は利益サプライズの分散の 4% 以下しか説明しない | 米国。古い | 古い標本 | 二次 |
| 遅れて発表する会社の反応 | 遅れた会社の発表日1日の異常リターンは**平均 −6.29%、中央値 −2.27%**（ライス大の要約、年・標本の詳細は**不明**）。サプライズを一定にしても、**遅れが 1 標準偏差（5 日）あたり、2 日窓で約 −21bp**（Springer の論文の要約）。同時発表の他社の影響も一部 | 米国。平均は一部の極端な例に引っ張られている可能性（中央値との差から）| 見つからず | 二次 |
| 予想日より 4 日以上遅れた会社 | 発表時の異常リターンがマイナス（予想日を使った研究の要約）| 米国 | — | 二次 |
| 日本: 発表時刻 | 音川・森脇: 取引時間**内**の発表は、時間外より株価反応が弱く、悪い知らせを含みやすい。標本は **2009 年 1 年間** | 日本。**期間が 1 年** | — | 二次 |
| 日本: 決算発表の延期 | 個人投資家向けの記事に事例（イズミ、2024 年のシステム障害で延期→下落傾向）。「延期した企業の約 7 割が 1 週間以内に下落」は出所の元資料を確認できず。**査読付きの実証は見つからなかった** | 日本。事例のみ | — | 二次・低 |
| 日本: 決算後のドリフト | 松村: Foster モデルで、サプライズの反応と数か月続くドリフト（PEAD）を扱う | 日本。発表日のずれの話ではない | — | 二次 |

### 2-2 結論（効きの大きさと私たちの条件）
- **米国の効きは大きい**（週 62〜138bp のアルファ）が、**日本の数字は見つからなかった**。
- 日本の大型株では、**ずれる会社自体が少ない**はず（推測）。遅れるのは不祥事・システム障害・監査の長期化など極端な理由が多く、これは「遅れ＝悪い」が強く出る反面、**標本が小さい**。
- 日本版を作るには「予定日」のデータが要る。適時開示の「決算発表日のお知らせ」「決算発表予定日の変更」を、**開示タイトル（155k 件）から拾える可能性**（要確認）。前年同期の発表日との差（日数）が、一番安く作れる物差し（日本の会社は毎年ほぼ同じ時期に出す、という前提は推測）。
- 指令役の「保留（論文を調査役が確かめてから）」に対しては、**論文は確かめた（米国で強い）が、日本の根拠は無い**、が答え。試すなら「事前登録→前年同期との日数差の上位群（遅い）と下位群の発表日リターン」を、**発表日のずれ自体は発表の 1〜2 週前に分かる**ことを使う。

## 3. 同業への情報の波及の効きの大きさ

**先に大事な点（私の第 5 回の読みを一部訂正）**: Thomas & Zhang (2008, JAR 46(4), 909–940) は、「同業の先発の良い知らせの後に、後発の株が**遅れて追随する**」ではなく、**過剰反応**を報告している。後発の会社の株価が先発の決算に反応した方向と、**後発の自社の決算発表日の反応が逆**になる（負の相関）。つまり先発の決算で動いた分は、**自社の決算で戻る**。これは第 5 回で私が「Thomas & Zhang は確認できず」と書いたものの中身で、**1A・1B の事前登録は「続伸」と「反転」の両側で書く必要がある**。

| 研究 | 内容 | 日本 | 信頼度 |
|---|---|---|---|
| Thomas & Zhang (2008) | 後発が先発の決算に動いた分は、自社の決算の発表日に**逆に戻る**＝過剰反応。**数字（%）は検索で取れなかった** | — | 二次 |
| 後続の追試 | 窓は **[−1,+1] の 3 日**が標準。「市場は先発の決算の同業への含意を過大評価し、後発の自社発表で修正される」と整理 | — | 二次 |
| **日本: Kitagawa (2014)、Kitagawa (2021, Japanese Accounting Review 11(1), 1–32)** | **日本の決算で過剰反応を確認**。製品市場の競争が強い業種ほど過剰反応が強い。競争が強いときは**悪い知らせの波及**の方が過剰反応が大きい。先発が「ベルウェザー（指標となる会社）」のとき、成長産業のときは弱い。係数・CAR の数字は**取れず** | **日本。ただし期間・大型株かは不明** | 二次 |
| 国際比較（PolyU の WP、「過剰反応か過小反応か」）| 国ごとの違いを検討。日本の結果は要約に**出ていなかった** | 不明 | 二次 |
| 過小反応側: Hoberg-Phillips の置換ピア、Ramnath (2002)「業界全体の利益に対する過小反応と予想修正後ドリフト」、アナリストの予想修正の波及（Baker ほか 2019: 上位十分位の先発修正で、後発の **5 日累積異常が 0.54% 高い**。**別の研究の数字**）| 方向は反対（続伸）。**どちらが勝つかは条件（競争・先発の格・コンセンサスの有無）次第** | 米国中心 | 二次 |
| Han, Wild & Ramesh (1989) | **業種のリターンを引くと、先発と同業の異常リターンは相関しない**（＝波及の多くは業種全体の動き）| 米国。古い | 二次 |
| Freeman & Tse (1992) | 同じ業種の決算が続くほど、追加の影響は**逓減** | 米国。古い | 二次 |
| ピアの確認的ニュースの累積（Lee-Byun-Kim 2026）| 同業の確認的な決算が積み重なった後は、自社の驚きへの反応が強まる | 米国 | 二次 |
| ボラの波及（Hann ほか 2019）| 先発のインプライド・ボラが同業のボラに伝わる。一次モーメントを除いても残る | 米国 | 二次 |

### 3-1 「何日で何 %」への答え
- **窓の標準は先発の発表日 [−1,+1] の 3 日**。日数と % の**確かな数字は今回見つからなかった**（Thomas & Zhang、Han ほか、Freeman-Tse の本文が未読）。
- 参考になる**唯一の数字**はアナリスト予想修正の波及の「5 日で 0.54%」だが、これは**決算ではなく予想修正**で、別の研究。
- 日本の数字は、Kitagawa の方向（過剰反応）が分かるだけで、**大きさは取れなかった**。
- **力の見積もり用の置き方（推測）**: 3 日窓の波及が数 bp〜数十 bp でも、日本の大型株の 5 日保有で費用 0.10% を超える見込みは低い。E1（修正 5% 以上、5 日）の標本が小さいことと合わせると、**検出力は低い**。結果が「判断保留」になる可能性を、事前登録に先に書いておくのが安全。

### 3-2 1A・1B の事前登録で使えること
1. **方向は両側**（続伸/反転）。さらに「反転」の検出は、**後発の自社の決算発表日を含む窓**で見る必要がある（Thomas & Zhang の定義）。5 日保有が自社の発表日をまたぐ銘柄は、またがない群と分けて集計する。
2. 先発の定義（その業種で**最初に**出した会社）を日本の決算集中日（3 月期の 5 月中旬）でどう置くか。同日に発表が集中すると「先発」が定義できない。Kitagawa は先発・後発を日で分けている（詳細は未確認）。
3. **業種のリターンを引く**（Han ほか）。引かないと業種全体の動きを波及と読む。1B−1A はこの点で良い設計。
4. 先発が「ベルウェザー（業種の代表）」か、業種の競争の強さで効きが変わる（Kitagawa）。

## 4. Qwen3-Embedding（0.6B）の学習データの締め切り

**結論: 公式の技術報告には、締め切りの日は書かれていない（今回の検索では見つからなかった）。**

| 確認できたこと | 内容 | 信頼度 |
|---|---|---|
| 公式の技術報告 | "Qwen3 Embedding"（arXiv 2506.05176、2025-06）: 基盤モデル Qwen3 から初期化し、**弱い教師あり（Qwen3 が生成した合成データ）の事前学習 → 約 1,200 万組の高品質な教師ありデータ → モデル統合**。0.6B / 4B / 8B。**データの締め切り日は書かれていない** | 二次（要約。本文未読）|
| 基盤モデル | HF のメタデータで `base_model: Qwen/Qwen3-0.6B-Base`。Qwen3 の事前学習は **36 兆トークン・119 言語**（Qwen3 技術報告 arXiv 2505.09388）。**こちらにも締め切り日の記載は見つからなかった** | 二次 |
| 第三者の推定 | 一覧サイトは Qwen3 を **2025-01（確信度「低」）**、別の早見表は Qwen3-14B/Qwen2.5-72B を **2023-12**。Qwen3-Next のモデルが「自分の締め切りは 2024-10」と答えた報告もある（自己申告で、Qwen 側は**自己申告は当てにならない**と述べている）| 二次・低 |
| 論文の公開日 | Qwen3 技術報告 2025-05-13（日付）、Qwen3 Embedding は 2025-06 公開。**締め切りはこれより前に決まっているはず**（当然の上限）| 二次 |

**使い方の結論**:
- **確実に言える範囲**: 学習の締め切りは **2025-05 より前**（モデルの公開日が上限）。推定の幅は **2023-12〜2025-01**（第三者）。
- 安全側の置き方（推測）: **2025 年以降のデータで評価するのが最も安全**だが、私たちの開示データ（2009〜）の大半はモデルが「知っている」期間に入る。方向を学習する検証では、**2025 年以降だけを最終判定**にする（提案 5 の通り）。類似度（1B のピア選び）だけに使う場合は、将来のリターンを知っていても類似度に混ざる経路は小さい（推測）。
- 公式の確定は、HF の `Qwen/Qwen3-0.6B-Base` / `Qwen3-Embedding-0.6B` のモデルカードか、Qwen3 技術報告の本文を、利用者が Mac で開いて確かめる（私は PDF を開けない）。

## 5. 出典（R6 で使ったもの）

EDINET:
- EDINET API 仕様書 Version 2（2026 年 6 月）https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf
- 同（2024 年 7 月版の写し）https://time2log.com/wp-content/uploads/2024/08/11_EDINET_API%E4%BB%95%E6%A7%98%E6%9B%B8%EF%BC%88version-2%EF%BC%89.pdf
- EDINET FAQ（閲覧期間 10 年）https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0090_001.html
- EDINET 利用規約 https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html
- 関東財務局 https://lfb.mof.go.jp/kantou/disclo/gaiyou.htm ／ 財務省系 OGB https://www.ogb.go.jp/-/media/Files/OGB/Zaimu/okizaimu/kigyo/20147kaitei/11kigyou.pdf ／ 税務会計解説 https://www.sn-hoki.co.jp/article/tamasters/tamaster1689360/
- EDINET 概要書（2026 年 6 月）https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140102.pdf ／ EDINET タクソノミの概要説明（2013 年 8 月・2020 年 11 月・2023 年 12 月）https://www.fsa.go.jp/search/20130821/1b_1.pdf , https://www.fsa.go.jp/search/20201110/1b-1_GaiyoSetsumei.pdf , https://www.fsa.go.jp/search/20231211/1b-1_GaiyoSetsumei.pdf
- 二次: 書類一覧の項目 https://zenn.dev/paradinight/articles/f4567f3728e4d2 , https://civinavi.com/guides/edinet-api , https://oryou-san.hatenablog.com/entry/2022/03/19/091130 ／ 本文の取り出し https://zenn.dev/robes/articles/81e18fb389fe01 , https://zenn.dev/robes/articles/4ab5a7f992000d , https://onto-logy.com/column/2110/ ／ キー取得 https://blog.systemjp.net/entry/2025/08/19/154734 ／ 回数の制限 https://time2log.com/ja/edinet/edinet-api%E3%81%AE%E5%88%A9%E7%94%A8%E5%88%B6%E9%99%90%E3%81%A8%E5%AF%BE%E7%AD%96%E3%81%AB%E3%81%A4%E3%81%84%E3%81%A6/

決算発表日のずれ:
- Johnson & So (2018) Time Will Tell（JFQA）https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/abs/time-will-tell-information-in-the-timing-of-scheduled-earnings-news/4C689E561BC34B1603A74EABA7F8BF59 ／ ワーキングペーパー https://www.wallstreethorizon.com/upload/SoandJohnsonTimeWillTellUpdatedOct2016.pdf ／ SSRN https://papers.ssrn.com/abstract=2480662
- Johnson & So (2018) Asymmetric Trading Costs（JAR、別の論文）https://onlinelibrary.wiley.com/doi/abs/10.1111/1475-679X.12189
- 遅い発表の悪い知らせ: https://business.rice.edu/wisdom/peer-reviewed-research/only-thing-market-likes-less-late-report-silence ／ https://link.springer.com/article/10.1007/s11156-021-01015-x ／ https://www.sciencedirect.com/science/article/abs/pii/0165410184900235
- 日本: 音川・森脇 https://www.jsda.or.jp/about/otogawaronbun.pdf ／ 松村（Foster モデル）https://www.jstage.jst.go.jp/article/jbef/3/0/3_0_1/_pdf/-char/ja ／ 個人向け記事 https://media.rakuten-sec.net/articles/-/26470

同業への波及:
- Thomas & Zhang (2008) JAR https://onlinelibrary.wiley.com/doi/10.1111/j.1475-679X.2008.00294.x ／ SSRN https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1028285
- Kitagawa (2021) https://www.jstage.jst.go.jp/article/tjar/11/1/11_1/_pdf/-char/en ／ SSRN https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2723928
- 国際比較 https://ira.lib.polyu.edu.hk/bitstream/10397/96101/1/Overreactior_UnderreactiIntra-Industry_Earnings.pdf
- Hann ほか 2019 https://link.springer.com/article/10.1007/s11142-019-9487-1 ／ 後続の整理 https://www.efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2017-Athens/papers/EFMA2017_0571_fullpaper.pdf , https://www.efmaefm.org/0EFMAMEETINGS/EFMA%20ANNUAL%20MEETINGS/2008-athens/papers/Kovacs.pdf , https://www.sciencedirect.com/science/article/abs/pii/S0378426615002241
- Ramnath 2002 https://www.researchgate.net/publication/228259157_Underreaction_to_Industry-Wide_Earnings_and_the_Post-Forecast_Revision_Drift

Qwen3:
- Qwen3 Embedding 技術報告 https://arxiv.org/abs/2506.05176 ／ Qwen3 技術報告 https://cdn.jsdelivr.net/gh/yanfeng98/paper-is-all-you-need/papers/00069-Qwen3_Technical_Report.pdf ／ HF Qwen3-Embedding-0.6B https://huggingface.co/Qwen/Qwen3-Embedding-0.6B ／ Qwen3-0.6B-Base https://huggingface.co/Qwen/Qwen3-0.6B-Base ／ 第三者の締め切り一覧 https://www.knowledge-cutoff.date/providers/qwen.html , https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Thinking/discussions/10

出典は上記で約 50 件。**PDF 本体を開いて確かめたものは 0 件**。

## 6. 見つからなかったもの
- EDINET 仕様書本文: 書類一覧の項目表、ステータスコード表（3-3）、書類種別コードの表（130 の表記）、`withdrawalStatus`・`docInfoEditStatus` の値、回数の上限の公式の数字、2026 年版の登録手順の電話の要否。
- 有報の本文が XBRL に載る最初の年度の公式の確認（2013-09 の対象拡大からという読みは推測）。
- 日本の決算発表日のずれの査読付き研究。Johnson & So の日本の追試。
- Thomas & Zhang の効きの大きさ（%）、Kitagawa の係数、日本の同業波及の日数・%。
- Qwen3 / Qwen3-Embedding の公式な学習データの締め切り日。

## 6. 追加確認（文章で近い会社のモメンタム／つながりモメンタム／Lazy Prices の買い側）

**ラベル**: 全て二次（検索要約。論文本体は未読）。数字は要約に出たものだけ。

### 6-1 Hoberg & Phillips「Text-Based Industry Momentum」（JFQA 53(6), 2355–2388, 2018-12）
| 項目 | 内容 | 信頼度 |
|---|---|---|
| 何を作ったか | 10-K の事業説明の類似度で決めた近い会社（**TNIC-3**: SIC の 3 桁と同じ細かさに調整）を同業とし、**自社を除く近い会社の月次リターンの等加重平均**を信号にする。比較は SIC・FF-48（時価加重）の業種。近い会社の**数は固定でなく、細かさの調整で決まる**（件数は取れず）| 二次 |
| リターンの期間・持つ期間 | 基本の信号は **t−12 〜 t−1 か月**。1 か月反転の影響を避けるため直近 1 か月を抜く変形がある。リバランスは**月次**（カレンダー時間のポートフォリオ）と読める。1 週間保有の月次アルファ **1.04%（有意水準 10%）**という第三者（Kai Wu の追試記事）の数字あり | 二次（保有期間の本文は未確認）|
| 効きの大きさ | 要約: 見えにくい近い会社のショックは**経済的に大きい**モメンタム利益を生み、**自社のモメンタムの変数より強い**。TNIC の説明力の改善が SIC に対し 25〜40% なのに対し、**モメンタム利益の改善は 100〜200%**。**月次アルファ・t 値の本数字は取れず** | 二次 |
| 標準の業種モメンタムとの差 | SIC の近い会社のショックは **1〜2 か月で伝わり、利益は小さく短命**。TNIC の近い会社のショックは**最長 12 か月ほど続く**（長持ち）。SIC-4 並み・SIC-3 並みに細かく揃えた TNIC でも、強い（TNIC-3 の月次アルファは 1% 水準で有意）。理由は「見えにくい近い会社には投資家の注意が向かない」 | 二次 |
| 発表後の追試・弱まり | 論文自体が公表後（out-of-sample）の予測力を調べているが、**要約では結果が見えなかった**。第三者の追試は Kai Wu の記事のみ。**公表後に弱まったかの独立の研究は見つからなかった** | 見つからなかった |
| 日本・米国以外 | **木村・中川「Industry Momentum Strategy Based on Text Mining in the Japanese Stock Market」（IIAI-AAI 2022, IEEE）**: 日本の**有価証券報告書の事業内容**を text mining して業種を作り、**従来の業種分類より、テキストによる業種分類のモメンタムの方が有意**（見えにくい同業群が先行・遅行を生む）。業種分類の比較図では「T33 が最も分類能力が高く、次に GICS、SCDV33、BOW33」（断片）。**効きの大きさ・期間・大型株の別は取れず**。Swinkels (2002) は、**従来の分類では日本の業種モメンタムはほぼ支持されない**とした（分類に敏感）。後続に Springer の "Hidden neighbours"（2024、株価ネットワークから業種モメンタムを取り出す）| 二次 |
| 注意 | 学会発表（IIAI-AAI）であり、査読の強さは JFQA と比べ弱い。日本は「従来の分類ではそもそも業種モメンタムが弱い」ので、TNIC の方が有意になる土台が違う（推測）| 推測 |

### 6-2 つながりのある会社のモメンタム（日本での追試）
| 研究 | 内容・数字 | 日本 | 信頼度 |
|---|---|---|---|
| Cohen & Frazzini (2008) 顧客モメンタム | 顧客のショック後に供給元が遅れて動く。3 因子・流動性・自社/業種モメンタムを入れても残る（SFAS 131 の 10% 以上の顧客）| 原論文は米国 | 二次 |
| Lee・Sun・Wang・Zhang (2019, JFE 132(3), 76–96) 技術の近さのモメンタム | 特許の技術分類で近い会社のリターンが自社を予測。**長短 月 117bp（等加重, t=5.47）／時価加重 月 69bp**。業種モメンタムと別物でリスク要因で説明しにくい。データは米国のみ。中国のヘッジファンドが追試した、と大学の記事 | **日本の追試は見つからなかった** | 二次 |
| 日本の顧客モメンタム | Bloomberg 白書（2012–2018, TPX が最強）、Yamamoto-Kawadai-Miyahara（日米で有効、長短年 4.5%）。前章（R5）の通り、**データが有償**で個人は取れない | 日本（期間短い）| 二次 |
| 米国の追試での弱まり | Pinchuk: 長短 月 1.22%（等加重）/1.06%（時価加重）。**発見後の標本では小さく有意でなくなる** | 米国 | 二次 |
| 経済的なつながりの日本の追試（技術・特許・テキスト以外）| **見つからなかった** | — | 見つからなかった |

### 6-3 Lazy Prices の「変えていない会社（買い側）」だけの効き
- **訂正（R5 の私の書き方）**: R5 で「188bp は極端な五分位の長短」と書いたが、正確には **188bp は「事業等のリスク（Risk Factors）」節だけの 5 因子アルファ（非変更 − 変更, t=2.76, 月次）**。文書全体（年次・四半期）の長短は **月 30〜60bp で有意**（Alpha Architect の要約）。
- **買い側だけの数字は、原論文の表を見ていないので取れなかった。** Alpha Architect は「**非変更群（Q5）の買いだけで、アルファの半分超が取れる**（Q5 の 5 因子アルファの数字は表にある、と書く）」と述べるが、**その数字（ブログ内の "23bp" は私の検索では確認できず）**は不明。つまり「買い側が半分超」は**ブログの要約**で、論文の表で未確認。
- 空売りができない私たち（買いのみ）にとって重要な含意（推測）: 文書全体の長短 30〜60bp/月の半分なら、**買い側は月 15〜30bp 程度**の見込み。年 2〜4%。費用は月次入れ替え・回転が低いので年 1.2% 以下だが、日本の大型株で再現するかは別問題。
- 注意: 買い側が「変えていない」会社は、市場全体を上回る理由が「悪い変更の会社を避けた」分に過ぎないかもしれない（空売りの代わりに買い側の質を見ている）。平均的な大型株との差で見る必要がある。

### 6-4 採用案（文章で近い会社のモメンタム、月 1 回）への注意点
1. 近い会社の定義は、**日本の有報「事業の内容」の埋め込みの近傍**（木村・中川は日本で同方向の結果）。件数 k を事前登録（結果を見る前に固定）。
2. 信号 = **近い会社の過去 t−12〜t−1 か月（またはその変形）の等加重リターンから自社と業種（33 業種）の動きを引いた分**。33 業種モメンタムとの差（HP の主張は 100〜200% の改善）が評価の核心。
3. **日本では従来の業種モメンタムが弱い（Swinkels）**ので、「33 業種のモメンタム自体がほぼゼロ」の可能性が高く、その場合は「差」でなく絶対水準が問われる。
4. 日本の追試は学会発表 1 本のみ。**弱まりの独立の研究は無い**。公表後の追試で劣化した顧客モメンタムの例（Pinchuk）があるので、**劣化を想定した小さめの期待**を事前登録に書く。
5. 先読み: 類似度の埋め込みに将来リターンは混ざりにくいが、有報の「事業の内容」は年ごとに変わるので、**その年に提出された有報の埋め込みだけで近さを作り、提出日以降に使う**（提出前に使わない）。

### 6-5 追加の出典
- Hoberg & Phillips, Text-Based Industry Momentum（JFQA）https://www.cambridge.org/core/services/aop-cambridge-core/content/view/6E04891A7AD30C24C21CBDE37EC06BF4/S0022109018000479a.pdf/text-based-industry-momentum.pdf ／ 著者版 https://faculty.tuck.dartmouth.edu/images/uploads/faculty/gordon-phillips/hoberg_phillips_TNICmomJFQA_FinV1.pdf ／ SSRN https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2504738 ／ ResearchGate https://www.researchgate.net/publication/328840724_Text-Based_Industry_Momentum
- Kai Wu による追試の記事 https://mrkaiwu.github.io/ind_momentum.html
- 木村・中川 日本の業種モメンタム（IEEE）https://ieeexplore.ieee.org/document/9894609/ ／ ResearchGate https://www.researchgate.net/publication/363801856_Industry_Momentum_Strategy_Based_on_Text_Mining_in_the_Japanese_Stock_Market ／ 比較図 https://www.researchgate.net/figure/Comparison-of-each-industry-classification-performance_fig1_363801856
- "Hidden neighbours"（Springer, 2024）https://link.springer.com/article/10.1007/s11408-024-00455-4
- Swinkels 2002（J. Asset Management）https://ideas.repec.org/a/pal/assmgt/v3y2002i2d10.1057_palgrave.jam.2240071.html
- Lee-Sun-Wang-Zhang https://www.sciencedirect.com/science/article/abs/pii/S0304405X18303167 ／ SSRN https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3036241 ／ CityU 記事 https://www.cb.cityu.edu.hk/CityBusinessMagazine/2020-Issue1/en/technology-links-and-predictable-returns
- Lazy Prices（NBER WP）https://www.nber.org/system/files/working_papers/w25084/w25084.pdf ／ JF 版 https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12885 ／ Alpha Architect https://alphaarchitect.com/2016/06/13/lazy-prices/ ／ Kent Daniel の討論資料 https://www.kentdaniel.net/discuss/2016/CMN.pdf

### 6-6 見つからなかったもの（6 章）
- TNIC モメンタムの月次アルファ・t 値、近い会社の数、保有期間の本文、公表後の弱まりの独立の研究。
- 木村・中川の効きの大きさ・期間・大型株の別。
- 技術の近さのモメンタム（Lee ほか）の日本の追試。
- Lazy Prices の買い側（非変更群）だけのアルファの数字。
