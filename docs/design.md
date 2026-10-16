# survey-coder 技術仕様

計画書の「設計案」タブ（https://claude.ai/artifact/12nKambwL4jB8cMUkHuZHx ）の技術仕様版です。
構築係・検品係はこの文書を正として実装・検品します。Mac の機種とメモリが決まるまで、
モデルと並列数は「候補」のままにし、架空データでの比較（8章）で確定します。

最終更新：2026-10-16

## 1. 全体の流れ

| 工程 | コマンド | 手作業 | 主な出力（案件フォルダ内） |
| --- | --- | --- | --- |
| 0 点検 | `survey-coder doctor` | ― | なし（結果を表示） |
| 0 案件作成 | `survey-coder job init <job-id>` | ― | `manifest.json`、`job.yaml` |
| 1 受け取り・伏せ字 | `survey-coder intake <job-id> --file <受領ファイル>` | 差し戻し判定の確認 | `00_input/`、`10_masked/` |
| 2 下ごしらえ | `survey-coder normalize <job-id>` | ― | `20_work/normalized.parquet` |
| 3 カテゴリ案 | `survey-coder propose-codeframe <job-id> [--question Q1]` | ― | `30_review/codeframe.draft.yaml` |
| 4 コード表の確定 | `survey-coder approve-codeframe <job-id> --file <yaml>` | 案の確認、依頼者の修正1回 | `30_review/codeframe.v1.yaml` |
| 5 全件分類 | `survey-coder classify <job-id>` | ― | `20_work/classified.parquet`、`llm_cache.sqlite` |
| 6 抜き取り検証 | `survey-coder review export` → 入力 → `survey-coder review score` | 約1時間の判定 | `30_review/review_sheet.xlsx`、`review_metrics.json` |
| 7 納品物の作成 | `survey-coder build-report <job-id>` | 納品前の目視 | `40_output/` |
| 8 納品と削除 | `survey-coder purge <job-id> --confirm <job-id>` | ― | 台帳への記録のみ |

- 実在の案件のコマンドは、Claude Code を起動していない別のターミナルで実行する。
- 各工程は入力のハッシュ・パラメータ・プロンプト版・モデル版から指紋（fingerprint）を作り、一致すればスキップする。`--force` で再実行。上流を再実行したら下流は stale にする。
- 出力は一時ファイルに書いてから `os.replace` で置き換える。案件ごとに lock ファイルで二重起動を防ぐ。

## 2. ディレクトリ

リポジトリの構成は README と「設計案」タブの図のとおり。主なモジュール：

```text
src/survey_coder/
├── cli.py              # Typer。1工程＝1サブコマンド。pretty_exceptions_show_locals=False
├── settings.py         # pydantic-settings（env_prefix="SC_"）。jobs_root はテスト中に実フォルダを指すと例外
├── logging_safe.py     # 回答本文を出さないログ設定。外部ライブラリのロガーは WARNING
├── netguard.py         # socket.connect を 127.0.0.1 / ::1 に制限。HF_HUB_OFFLINE を設定
├── job/                # paths, manifest, lock, fingerprint
├── intake/             # reader（文字コード、列の許可リスト）, pii_regex, pii_names, pii_mask, pii_report
├── text/               # normalize（NFKC → neologdn）, junk_rules
├── embed/              # encoder（sentence-transformers、.npy キャッシュ）, dedup
├── cluster/            # cluster（UMAP＋HDBSCAN / KMeans）, bundle（LLM に渡す束）
├── llm/                # client（127.0.0.1 のみ）, schema（JSON Schema）, cache（SQLite）, prompts
├── codeframe/          # model（pydantic）, io（YAML）
├── review/             # sample（抜き取り）, sheet（検証シート）, score（指標と Wilson 区間）
├── report/             # tallies, excel_writer, charts, quotes, accuracy
└── steps/              # intake, normalize, propose, approve, classify, review, report, purge, doctor
```

案件フォルダ（リポジトリの外。`~/fukugyo-c-jobs/<job-id>/`）：

```text
<job-id>/              # 例 C2610-001（C＋年月＋連番。顧客名は入れない）
├── manifest.json      # 状態・ハッシュ・モデル版。回答本文と氏名は入れない
├── job.yaml           # メニュー、設問IDと列名の対応、ID列、属性列、締切
├── 00_input/          # 受領原本（chmod 400）
├── 10_masked/         # answers_masked.parquet、pii_report.xlsx（ローカル専用・納品しない）
├── 20_work/           # normalized.parquet、embeddings.npy、clusters.parquet、llm_cache.sqlite、classified.parquet
├── 30_review/         # codeframe.draft.yaml、codeframe.v1.yaml、review_sheet.xlsx、review_metrics.json
├── 40_output/         # 納品Excel、charts/*.png
└── 90_logs/           # 回答本文を含まないログ
```

テキストは3層で持つ：`text_raw`（原本。00_input にだけある）、`text_masked`（伏せ字済み。引用と LLM 入力の元）、
`text_norm`（正規化済み。埋め込みと分類用）。納品物の引用には必ず `text_masked` を使う。

## 3. 環境

- Python 3.14（`.python-version`）。`requires-python = ">=3.14,<3.15"`。uv 0.13 の既定は 3.15 で torch が入らない。
- `[tool.uv] environments` は darwin-arm64 と linux（クラウドでの検品用）の両方。`required-environments` で darwin-arm64 の wheel を必須にする。
- `[tool.uv] constraint-dependencies = ["sudachipy>=0.6.11,<0.7"]`。GiNZA 5.3.0 は SudachiPy 0.7 未満が必要。`sudachidict-core` は直接依存に入れない（入れる場合は `==20260723`）。
- torch 2.12 以降の macOS wheel は macOS 14 以上が必要。macOS 13 の場合は `torch==2.11.0` に固定する。
- 主な依存（2026-10 時点の版）：pandas 3.0、pyarrow、python-calamine、phonenumbers 9.0、ginza 5.3.0 / ja-ginza 5.3.0、neologdn 0.5.6、emoji、sentence-transformers 6.1、torch 2.14、scikit-learn 1.9、umap-learn 0.5.12、hdbscan 0.8.44、openai（llama-server 接続）、pydantic 2、pydantic-settings[yaml]、typer、xlsxwriter 3.2.9、openpyxl、matplotlib 3.11、scipy。開発用：pytest、pytest-socket、faker、ruff。
- pytest の addopts：`--disable-socket --allow-hosts=127.0.0.1,::1`。接続先は常に `http://127.0.0.1:<port>` と書く（`localhost` はテスト中に止まる）。
- ruff：`select = ["E","F","I","B","UP","T20"]`、`target-version = "py314"`。

## 4. 工程ごとの仕様

### 4.1 受け取り（intake）

- 文字コードは `utf-8-sig` で読み、失敗したら `cp932`。`shift_jis` は使わない。採用した文字コードを manifest に記録。
- xlsx / xls は `pandas.read_excel(engine="calamine")`。CSV は `dtype=str, keep_default_na=False`。
- 読む列は job.yaml の許可リスト（回答ID、自由回答列、属性列）だけ。それ以外はメモリにも載せない。
- ヘッダが `/氏名|名前|フリガナ|ふりがな|メール|mail|e-?mail|電話|TEL|携帯|住所|郵便|〒|生年月日|誕生日|会員番号|顧客番号/i` に当たる列、
  または値の30%以上で連絡先の個人情報が見つかる列は「個人情報の列」として取り込みを止め、差し戻す。
- 回答ID列がなければ intake 時に連番を振って固定する。answer_id = 回答ID ＋ 設問ID。

### 4.2 伏せ字（PII）

検出は NFKC 後のテキストに対して、次の順に行う（先に見つけたものを優先し、重なりは長い方）。

1. URL：`(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+`（ASCII だけ。後ろの日本語を巻き込まない）
2. メール：`[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}`、加えて `(at)` `[at]` `アットマーク` 表記
3. 電話：`phonenumbers.PhoneNumberMatcher(text, "JP", leniency=Leniency.VALID)`。補助に `(?<!\d)0\d{9,10}(?!\d)`（英字が隣接する場合の取りこぼし用）
4. SNS：`(?<![A-Za-z0-9_@.])@[A-Za-z0-9_]{3,30}` と「LINE ID」の文脈
5. 郵便番号：`〒\s*\d{3}DASH?\d{4}`（確度高）、前後に数字がない `\d{3}DASH\d{4}`（確度中）
6. 住所：都道府県名＋市区郡町村、または「〇〇市／区／郡」の後に番地（n丁目、n番地、n号、n-n、n-n-n）が続く場合だけ。「渋谷区のお店」のような地名だけの言及は伏せない
7. 生年月日：日付の後に「生まれ・生・誕生」、または「生年月日・誕生日」の後に日付
8. 12桁番号：`(?<!\d)\d{4}DASH?\d{4}DASH?\d{4}(?!\d)`。チェックディジットが合い、かつ近くに「マイナンバー・個人番号」があれば [個人番号]、なければ [番号]
9. カード番号：13〜19桁で Luhn が合い、ブランドの先頭番号と桁数が整合し、近くに「カード・クレジット」があれば [カード番号]、なければ [番号]
10. 人名：GiNZA（`spacy.load("ja_ginza")` と明示）の Person、同じ解析の Sudachi 品詞 `名詞-固有名詞-人名-*`、敬称ルールの和集合。
    敬称ルールは「さん・様・君・くん・ちゃん・氏・先生・部長・課長・係長・店長・主任・社長・院長・先輩・殿」の直前が人名・固有名詞・未知語・大文字1字なら候補。
    直前が普通名詞（スタッフ、店員、看護師など）なら除く。Person の範囲に敬称が含まれていれば敬称を外してから伏せる

`DASH` は `[\-‐‑‒–—―−－ー]`（IME の長音「ー」を含む）。

- 伏せ字トークン：[氏名] [電話番号] [メール] [URL] [SNSアカウント] [郵便番号] [住所] [生年月日] [個人番号] [カード番号] [番号]。敬称は残す（例：[氏名]さん）。通し番号は付けない。
- 確信度：2つ以上の検出が一致、または敬称付きなら high。1つだけなら low。low は個人情報レポートで目視確認する。
- 判定の段階（`config/defaults.yaml` で変更可）
  - A 即時停止：文脈語つきの個人番号・カード番号、または個人情報の列。処理を止め、依頼者に連絡して受領データを削除する
  - B 差し戻し推奨：連絡先（電話・メール・番地までの住所・生年月日・SNS）を含む行が有効回答の1%超、または10行以上
  - C 自動で伏せて続行：人名。ただし人名を含む行が10%を超えたら、署名欄ではないかを依頼者に確認する
- 個人情報レポート（`10_masked/pii_report.xlsx`）は生の個人情報を含むので、案件データと同じく納品後に削除する。依頼者向けには件数だけを精度報告に載せる。

### 4.3 下ごしらえ（normalize）

- v1（表示・引用・伏せ字用）：NFKC → URL とメールの検出 → `neologdn.normalize(s, tilde="normalize_zenkaku", remove_space=False)`
  → 制御文字とゼロ幅文字の除去（ZWJ と VS16 は絵文字のために残す）→ 改行を `\n` に統一、連続空白を1つに、前後の空白を削除。
- v2（LLM・埋め込みの入力）：伏せ字済みの v1 から、改行を ` / ` に置き換え、`neologdn` の `repeat=3` で過剰な繰り返しを縮める。
- 無回答・無効の判定（記号・空白・絵文字を除いた「芯」に対する完全一致）。行は削除せず、理由コードを付けて残す。
  - 空欄 → 無回答（集計対象外）
  - 記号のみ、かな・英字1字、数字のみ、同じ文字の繰り返し、test・テスト・asdf などのテスト入力、母音のない英字4字以上 → 無回答（判読不能、集計対象外）
  - `^(特に|とくに)?(なし|無し|ナシ|ない|無い|ありません|ございません|思いつかない)(です|でした)?$`、none・no・n/a → 「特になし」（LLM に回さない）
  - わからない・分からない・不明・覚えていない・どちらともいえない → 「わからない」
  - 漢字1字（神・良など）、絵文字だけ、◎○△× だけ → LLM に回すが要確認の印を付ける

### 4.4 カテゴリ案（propose-codeframe）

- 埋め込み：第一候補 `intfloat/multilingual-e5-large-instruct`（入力の先頭に `Instruct: Identify the topic or theme of the given survey response\nQuery: `）。
  比較候補 `cl-nagoya/ruri-v3-310m`（先頭に `トピック: `）、`Qwen/Qwen3-Embedding-0.6B`。いずれも `normalize_embeddings=True`、device は mps。
  モデルはリビジョンを固定し、取得は事前に済ませ、処理中は `HF_HUB_OFFLINE=1`。埋め込みが終わったらモデルを破棄し `torch.mps.empty_cache()` してから LLM を使う。
- 重複：正規化後の完全一致をまとめ、cos ≥ 0.95（0.93〜0.97 で較正）を union-find でつなぐ。5文字以下は完全一致だけ。クラスタリングは一意の文単位で行い、件数を重みとして持つ。
- クラスタリング：UMAP（n_neighbors=15（1,500件以下）/30、n_components=10、min_dist=0.0、metric="cosine"、random_state=42）→ hdbscan
  （min_cluster_size は {max(8,N//150), max(10,N//100), max(15,N//60)}、min_samples は {3,5}、cluster_selection_method="eom"）。
  クラスタ数8〜35・外れ率40%以下のうち relative_validity_ が最大のものを採る。満たさなければ KMeans（k=min(30, N//30)、n_init=10、random_state=42）で多めに分けて LLM で統合する。
- 外れた回答：最近傍クラスタとの類似度がそのクラスタの下位10%以上なら仮に割り当て、残りから最大40件（乱数の種は固定）を LLM に見せて新カテゴリ候補を聞く。
- LLM に渡す束（クラスタごと）：件数と割合、重心に近い8件、無作為4件、遠い2件。各回答は120字で切る。
  出力スキーマ：`{label（15字以内）, definition（1〜2文）, include_examples（回答IDで返す）, exclude_rules, polarity, is_mixed_topic, split_suggestion}`。
  2回目の呼び出しで全クラスタを統合・改名し、8〜25コード＋「その他」＋「特になし・わからない」にする。
- この工程だけは思考モードを使ってよい（1案件で数回、max_tokens 8K 程度）。

### 4.5 全件分類（classify）

- llama-server の起動（モデル取得は顧客データを扱わない日に済ませる）：

  ```bash
  llama-server -m <model.gguf> --host 127.0.0.1 --port 8080 --offline --reasoning off \
    -c <スロット数×4096〜8192> -np <2〜4>
  ```

  v0.7.0 から既定ポートが 9931 に変わったので `--port` は必ず指定する。版は semver のタグで固定し、版・GGUF のファイル名・SHA256 を manifest に記録する。
- 呼び出し：openai SDK（`base_url="http://127.0.0.1:8080/v1"`）、`temperature=0`、`max_tokens=64`、
  `response_format={"type":"json_schema","json_schema":{"name":"coding","strict":True,"schema":SCHEMA}}`、
  `extra_body={"chat_template_kwargs":{"enable_thinking":False}}`。
- スキーマ：`{"labels": array(enum=コード一覧, minItems=1, maxItems=3), "confidence": enum(high, medium, low)}`、`additionalProperties: false`。
  コード一覧には「その他」と「判別不能」を必ず含める。重複は Python 側で除く。
- system メッセージ（コード表、分類規則、例）は全リクエストで1文字も変えず、回答本文と ID は user メッセージの末尾に置く（プロンプトキャッシュを効かせる）。
- 受け取った JSON は pydantic で再検証し、失敗は2回まで再試行、それでも失敗したら要確認に回す（キャッシュしない）。
- 起動時の自動テスト：思考の出力（reasoning_content）が空で、finish_reason が length でないこと。Gemma 4 でも行う。
- キャッシュ：`20_work/llm_cache.sqlite`（WAL）。key = sha256(answer_id | sha256(text_norm) | prompt_version | model@sha256 | codeframe_sha | 温度・種)。50件ごとに commit。
- 重複グループは代表の1件だけ分類し、結果を全員に展開する。
- 高速化（任意）：1リクエストに10〜20件をまとめる方式。items の id を enum にし、minItems=maxItems=件数。精度が1件ずつと同等なことを比較で確かめてから使う。
- メモリ帯ごとの初期値（要実測）：16GB は `-np 1〜2`・スロット 4K、24GB は `-np 2`・4K・KV キャッシュ q8_0、32GB 以上は `-np 4`・8K。

### 4.6 抜き取り検証（review）

計画書の「カテゴリごとに偏りなく」から変更する。均等抽出は重み付けしない限り全体の正解率を偏らせるため。

- A（盲検）：有効回答から無作為に100件（乱数の種を記録）。検証シートに AI の結果を載せず、作業者が分類1〜3をドロップダウンで選ぶ。
- 照合：スクリプトが A の不一致だけを「照合」シートに出し、作業者が「AI が正しい／自分が正しい／両方可／両方誤り」を選んで正解を決める。
- B（確認）：A を含めて検査数が8件未満のカテゴリだけ、そのカテゴリが付いた回答から無作為に補充し、AI のラベルを見せて正誤を付ける（40〜60件）。
- 合計140〜160件、約1時間（推定）。スタンダード（3問）の配分は未決（9章）。
- 指標（依頼者向け）：回答単位の正解率（両方可は一致扱い）と Wilson の95%信頼区間（`scipy.stats.binomtest(k, n).proportion_ci(method="wilson")`）、
  付け間違いなし率、付け漏れなし率、カテゴリ別の当否を件数で（例 9/10）。全体の指標は A だけから出し、B はカテゴリ別にだけ使う。
- 社内の基準：正解率85%未満、または検査数8以上で適合率70%未満のカテゴリがあれば、定義とプロンプトを直して再分類し、30件を追加検査する。
- 検証シート：`A_盲検`（No｜回答ID｜回答｜判定1｜判定2｜判定3｜メモ）、`B_確認`（No｜回答ID｜回答｜AI のカテゴリ｜判定｜正しいカテゴリ｜メモ）、
  `照合`（自動生成）、非表示の `codes`。ドロップダウンは `=codes!$B$2:$B$31` の範囲参照。保護は `protect("", {"autofilter": True, "sort": True})` で入力セルだけ解除。

### 4.7 納品物（build-report）

- Excel は XlsxWriter（`strings_to_formulas=False`、`strings_to_urls=False`）。ライトのシート：①はじめに ②コード表 ③単純集計 ④回答別分類 ⑤代表的な回答 ⑥精度レポート。
  スタンダードは③〜⑤を設問ごとに分け、`Q1_クロス_年代` などのクロス集計シートを足す。
- 集計：回答者ベース%（分母＝有効回答者数、MA）を主、回答件数ベースを参考列。脚注に「合計は100%を超えます」。
  空欄・判読不能は分母から外して件数を別に示し、「特になし・わからない」は分母に含める。
- 丸め：「小数第2位を四捨五入し、小数第1位まで表示」。セルには丸める前の比率を書き、書式 `0.0%` で表示。文字列にするときは `Decimal` の ROUND_HALF_UP。
- グラフ：Excel のネイティブ横棒（`set_y_axis({"reverse": True})`、`data_labels: {"value": True, "num_format": "0.0%"}`）。
  同じ図を matplotlib で PNG にも出す（BIZ UDPGothic を `addfont`、`MPL_IGNORE_SYSTEM_FONTS=1`、dpi 200）。
- クロス集計：行%、全体より5pt以上高いセルは薄い青、5pt以上低いセルは薄い赤。n<30 の行は灰色で「参考値」。n<5 のセルは伏せる。
- 代表的な回答：候補は単一ラベル・15〜150字・伏せ字トークンなし・個人情報の正規表現や人名に当たらない・ほぼ重複でない回答。
  カテゴリ重心に近い20件から MMR（diversity 0.35）で3〜5件。LLM を使う場合は回答 ID だけを返させ、本文は元データから取る（引用が原文と完全一致することをテストする）。
- 精度レポート：検査方法1文、正解率と信頼区間（大きく表示）、付け間違いなし率・付け漏れなし率、カテゴリ別の表、誤りの傾向と対応、限界（抜き取り、作業者1名、小さいカテゴリは参考値）。
  信頼区間の説明は「同じ方法で抜き取り検査を繰り返すと、約95%の回でこの範囲に全件の本当の正解率が入る、という目安です」とし、「ほぼ確実」とは書かない。

### 4.8 削除（purge）

- 納品済みを確認してから、案件フォルダ（または案件ごとの暗号化ディスクイメージ）を削除する。上書き消去（`rm -P`）には頼らない。
- 暗号化ディスクイメージを使う場合：作成時のパスフレーズは乱数で作り、キーチェーンに保存しない。削除時は detach → イメージ削除 → パスフレーズ破棄。
  `hdiutil` の構文は Mac 実機の `man hdiutil` で確認してから実装する。
- 台帳 `~/fukugyo-c-jobs/_ledger/purge_log.csv` には案件ID・日時・件数・方法だけを書く。

## 5. 通信と安全

- 処理プロセスの起動時、huggingface_hub を import する前に `HF_HUB_OFFLINE=1`、`HF_HUB_DISABLE_TELEMETRY=1` を設定する。
- `netguard` で `socket.connect` を 127.0.0.1 と ::1 に制限する。LLM の接続先が loopback 以外なら起動時に止める。
- `doctor` が確かめること：FileVault（`fdesetup status`）、案件フォルダの場所（iCloud 同期下でない）、Time Machine の除外、llama-server が 127.0.0.1 だけで待ち受けていること、
  HF_HUB_OFFLINE、macOS の版、MPS が使えること、メモリ容量。
- Claude Code 側の遮断は `ops/claude-guard/README.md`。

## 6. 架空アンケート（見本・検品用）

### 6.1 内容

- 架空の「製造業A社の社員満足度調査」1,000名。設問：Q1 職場で改善してほしいこと（主）、Q2 この会社で働いていてよかったと感じること、Q3 会社・経営へのご意見（任意）。
- 属性の分布：部署（製造35／営業20／開発・技術12／管理部門13／物流10／CS10%）、年代（20代以下18／30代24／40代27／50代23／60代以上8%）、
  雇用形態（正社員72／契約12／パート・アルバイト10／派遣6%）、役職（一般70／主任・係長20／課長以上10%、年代と相関）、総合満足度1〜5（8／17／35／30／10%）。
  勤続年数は年代ごとの条件付き分布。20代×10年以上、パート・派遣×課長以上は0。
- 回答の種類：Q1 中身あり80／特になし11／空欄6／無効3%、Q2 70／15／12／3%、Q3 45／20／33／2%。満足度5の人は特になしの重みを2倍。
- Q1 のコード（中身のある回答に占める主ラベルの割合）：人員不足・業務量15／給与・賞与13／評価制度・昇進11／上司・マネジメント9／部署間連携・コミュニケーション8／
  残業・労働時間7／休暇の取りやすさ6／教育・研修・キャリア6／職場環境・設備5／ITツール・業務効率化5／福利厚生・手当4／柔軟な働き方3／
  経営方針・情報共有2.5／ハラスメント・人間関係2.5／その他3%。最小カテゴリでも20件以上。
- Q2：同僚・人間関係20／仕事のやりがい15／休暇・WLB12／雇用の安定11／福利厚生9／成長・学び8／上司7／柔軟な働き方6／製品・社会貢献への誇り5／給与・待遇4／その他3%。
- Q3：経営・ビジョンへの要望18／待遇改善16／人材育成・採用14／働き方・制度13／組織風土12／設備・IT投資8／本調査への意見8／感謝・応援7／その他4%。
- 複数カテゴリ：1つ65／2つ27／3つ8%。2つ目は60%の確率で関連ペア表（人員不足↔残業・休暇、給与↔評価・福利厚生、上司↔ハラスメント・評価、柔軟な働き方↔ITツール 等）から選ぶ。「その他」は単独のみ。
- 割り付け：カテゴリごとの件数を最大剰余法で確定 → 属性効果の乗数を掛けた重み行列を IPF で周辺分布に合わせる → 小さいカテゴリから重み付き非復元抽出。
- 属性効果（planted_effects.yaml）：製造×職場環境・設備×3.0、営業×残業×2.0・人員不足×1.5、20代以下×教育・研修×2.5・給与×1.5、40〜50代×評価制度×1.8、
  非正規×福利厚生×2.5・情報共有×2.0、30代×柔軟な働き方×2.0、管理部門×ITツール×2.5、満足度1〜2×上司×2.0・ハラスメント×3.0、勤続1年未満×教育×2.0。
- 感情（Q1）：要望50／不満38／強い怒り5／肯定まじり7%。文体：丁寧語40／常体・体言止め28／口語14／箇条書き8／強い口調6／絵文字・顔文字4%。
  文字数：10字以下12／11〜30字28／31〜80字35／81〜200字20／201〜400字5%（Q2 はやや短く、Q3 はやや長く）。指定帯の±20%を外れたら作り直す。
- 難しい回答（中身のある回答の15%）：キーワードはあるが別カテゴリ、否定、曖昧、皮肉、2話題の弱い混在、設問違い。正解ファイルに difficulty と trap_type を記録。
- 人物設定：属性に加え、子育て中・介護中・夜勤あり・中途入社・通勤90分・育休明け・外国籍（日本語やや不慣れ）などの特徴を1〜2個、乱数の種で決める。

### 6.2 作り方

1. 仕様表（`tools/samplegen/spec.py`、乱数の種 20261016）：正解ラベル・属性・回答の種類・文字数帯・文体・感情・難しさを決める。LLM にラベルは書かせない。
2. 人手の見本：ユーザーが Q1 を中心に30〜50件書く（source=human）。毎バッチ3件を文体の参考として示す。
3. 本文：Claude Code の対話セッションで `sample-writer` に20〜25行ずつのバッチを渡す。プロンプト・応答は `raw_llm/` に保存して凍結し、以後は作り直さない。
   Python から `claude -p` を大量に呼ぶ自動化はしない（個人プランの規約への配慮）。自動化する場合は API キーと Batch API に切り替える。温度は変えられない前提で、多様さは行ごとの指定で作る。
4. バッチの自動チェック：row_id の欠落なし、文字数帯、個人情報らしき文字列がない、文体の簡易判定、禁止フレーズがない、既存と似すぎていない。
   全体で2%以上の回答に出る言い回しは以後の禁止フレーズに足す。
5. ノイズ（行ごとの独立な確率、`noise_log.jsonl` に記録、ラベルは変えない）：全角英数15%、半角カナ2%、表記ゆれ辞書25%（有給／有休／年休、残業／時間外、上司／上長、給料／給与、PC／パソコン、テレワーク／リモート など）、
   誤変換4%（異動→移動、保障→保証、以外↔意外 など）、記号の連続5%、改行・全角空白8%、機種依存文字3%。「9時〜18時」「2〜3年」などの範囲表現を必ず含める。
6. 個人情報（全ノイズの後に入れ、最終テキスト上の位置で記録）：約40行・約55か所。氏名30（姓＋役職18、姓＋さん6、フルネーム4、カナ・ローマ字2）、電話8、メール8、社員番号5、その他4。
   紛らわしいが個人情報でないもの（「品川の倉庫」「A課長」「年収400万円」「2026-10-16」など）を15件。
   電話は `03-0XXX-XXXX`・`090-0XXX-XXXX`・`070-0XXX-XXXX` だけを使い、全番号で `phonenumbers.is_valid_number` が False になることをテストする。メールは `example.*` だけ（Faker の `email(domain=...)` 可、`company_email` と `free_email` は不可）。
7. 重複：中身のある回答のうち文字3-gram の Jaccard ≥ 0.5、または埋め込みの cos ≥ 0.92 なら後の行を同じ仕様で作り直す（削除すると割合が崩れるため）。短い回答と「特になし」は対象外。
8. 監査：`qa-checker` が正解を見ずにラベルを付け、不一致と無作為100件をユーザーが裁定する（1.5〜2時間）。結果を audit_status に記録。
9. 凍結：行を乱数で並べ替え、ID を R0001〜R1000 に振る。`manifest.json`（種、生成モデル、CLI 版、ライブラリ版、プロンプトの sha256、日付）と `checksums.sha256` を作り、タグ `sample-es-v1` を付ける。

### 6.3 ファイル

```text
samples/fictional/es_survey_v1/
├── README.md            # 「本データはすべて架空です」、作り方、用途（評価・デモ専用、モデル学習に使わない）
├── manifest.json / checksums.sha256
├── input/               # survey_input.xlsx と survey_input.csv（UTF-8 BOM、CRLF）。依頼者が送る形。正解は含めない
├── ground_truth/        # rows_gt.csv、labels_long.csv、pii_spans.csv、noise_log.jsonl、codeframe_q1〜q3.csv、planted_effects.yaml
├── raw_llm/             # 生成時のプロンプトと応答（凍結）
├── qa_variants/         # 個人情報だらけの100行（差し戻しの期待）、cp932 版、BOM なし版、ヘッダ違い（公開しない）
└── expected/            # 回帰比較用の期待値
```

- 入力の列：回答者ID, 部署, 年代, 勤続年数, 雇用形態, 役職, 総合満足度, Q1_職場で改善してほしいこと, Q2_働いていてよかったと感じること, Q3_会社へのご意見・ご要望。
- パイプラインは `input/` だけを読む。`ground_truth/` を読んだらテストを失敗させる。
- 検品の指標：カテゴリ別の適合率・再現率・F1、完全一致率（人手部分と合成部分を分けて集計）、個人情報の種類別再現率と紛らわしい語の誤検出数、
  コード表案が正解カテゴリをいくつ再現したか、仕込んだ属性効果がクロス集計で見えるか、抜き取りの信頼区間が全件の正解率を含むか。
- 3,000件規模の速度測定は、1,000件を3回複製して ID とノイズを振り直したもので行う（精度評価には使わない）。

### 6.4 公開時の表示

出品画像・PDF のフッターに「本サンプルは架空の調査データ（AIで作成した架空回答1,000件）を用いた見本です。実在の企業・団体・個人とは関係ありません」。
精度の数字には「本サンプルでの一致率で、実データでの精度を保証するものではありません（納品時は毎回抜き取り検証の結果を報告）」を添える。
個人情報は架空でも原文を見せず、伏せ字後の表示と件数だけを出す。

## 7. Claude Code の使い方

- サブエージェントの定義は `.claude/agents/`。呼び出し時に model・effort を渡さない。
- 検品係は Bash を持つので、作業の前後で `git status --porcelain` を比べて変更がないことを確かめる。
- 顧客データの遮断は3層（読み取り禁止ルール、OS のサンドボックス、フック）。管理設定の設置と見張りファイルでの確認は `ops/claude-guard/README.md`。

## 8. モデルの比較（Mac 確定後）

架空データ（6章）で、次を同じ手順にかけて表にまとめ、`docs/benchmarks.md` に残す。

- LLM：メモリ帯の第一候補と代替 × 量子化（Q4_K_M / Q6_K / Q8_0）× 並列数（-np 1/2/4）。
  指標は micro/macro F1、完全一致率、JSON の妥当率、生成 tok/s、総処理時間、ピークメモリ。思考オフの本番条件で測る。
- 埋め込み：multilingual-e5-large-instruct、ruri-v3-310m、Qwen3-Embedding-0.6B。指標は正解カテゴリとの ARI と V-measure、所要時間、ピークメモリ。

メモリ帯ごとの候補（推定。実測で確定）：

| メモリ | LLM 第一候補 | 代替 |
| --- | --- | --- |
| 16GB | Gemma 4 12B（4bit QAT）／Gemma 4 E4B | Qwen3.5-9B（思考オフが効くことを確認してから） |
| 24GB | Gemma 4 26B-A4B（Q4_K_M。3bit には下げない） | Gemma 4 12B（8bit） |
| 32〜36GB | Gemma 4 26B-A4B（Q4_K_M〜Q6_K） | Qwen3.6-35B-A3B（4bit） |
| 48GB 以上 | Gemma 4 26B-A4B（Q8_0） | Gemma 4 31B（確信度の低い回答の再判定） |

## 9. 未決事項

- Mac の機種・チップ・メモリ・macOS の版（モデル、並列数、torch の版が決まる）
- 管理設定（sudo）を置けるか
- Mac に Excel があるか（検証シートと納品物の確認）
- 抜き取り検証の方式変更の承認と、スタンダード（3問）での配分（1問あたり60〜70件案）
- 見本の業種（製造業でよいか）、EC レビュー300件を後から作るか
- 見本の本文を Claude の対話セッションで書くか、ローカルLLM で書くか
