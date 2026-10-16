# データシートのプレースホルダ

`DATASHEET.ja.template.md` と `DATASHEET.en.template.md` にある `{{名前}}` の一覧です。2つのテンプレートは、同じ83個のプレースホルダを使っています。

## 記入のルール

- **日英で同じ値を入れる。** 値は数字・コード・日付・名前・メールアドレスだけにしてあり、言語によって変わりません。値は1つのJSON（キー = プレースホルダ名）にまとめ、両方のテンプレートを同じJSONで埋めます。
- **件数**：整数。1,000以上は3桁ごとにカンマを入れる（例 `6,512`）。
- **割合**：`%` を付けない数字で、小数第1位まで（例 `4.0`）。`%` はテンプレート側にあります。丸めは四捨五入です（Python なら `Decimal` の `ROUND_HALF_UP`。`round()` は偶数丸めなので使わない）。分類ごとの割合は、丸めのため合計が100.0にならないことがあります。
- **日付**：`YYYY-MM-DD`。**年月**：`YYYY-MM`。
- **誤り率の分母**は抽出した行数（`verification_sample_size`）、**それ以外の割合の分母**は全行数（`row_count`）です。
- 出どころが「metadata.jsonl」のものは、出荷する metadata.jsonl から計算します（手で数えない）。検証の数値も、出荷する metadata.jsonl の `verification` 欄から計算します。

## 一覧

### 概要と連絡先（梱包係・手入力）

| プレースホルダ | 意味 | 値の出どころ | 形式 | 例 |
|---|---|---|---|---|
| `dataset_name` | データセットの名前 | 手入力 | 文字列（日英共通の名前） | `Japan Travel Scenes Captions` |
| `dataset_version` | この版の番号 | 手入力 | `X.Y.Z` | `1.0.0` |
| `release_date` | この版の公開日 | 手入力 | 日付 | `2026-11-30` |
| `row_count` | 行数（= 画像の枚数） | metadata.jsonl の行数 | 件数 | `1,000` |
| `image_long_edge_min_px` | 納品画像の長辺の最小値 | metadata.jsonl：各行の `max(image_info.width, image_info.height)` の最小 | 整数（カンマなし） | `2048` |
| `image_long_edge_max_px` | 納品画像の長辺の最大値 | 同上の最大 | 整数（カンマなし） | `2048` |
| `licensor_name` | 撮影者・著作権者・許諾する人の名前 | 手入力（契約書と同じ表記） | 文字列 | `Example Photo Office` |
| `contact_email` | 問い合わせ・削除依頼の窓口 | 手入力 | メールアドレス | `dataset@example.com` |
| `license_reference` | 拘束力のある契約書やライセンスの名前 | 手入力（販売先ごと）。記入の前に、本人がその販売先の拘束力のあるライセンス・契約に 6 の6項目（非独占、機械学習の学習・微調整・評価に限る、再配布・転売・サブライセンス不可、ストック・報道・展示用の利用不可、人物や正確な場所の特定不可、著作権は撮影者に残る）がすべて含まれることを確かめる。1つでも欠ける販売先には出さないか、6 をその販売先用に書き直して本人が了承する | 文字列 | `Dataset License Agreement v1.0` |
| `takedown_response_days` | 削除依頼に返信するまでの日数 | 手入力 | 整数 | `7` |

### AI生成の表示と誤り率（検証係）

| プレースホルダ | 意味 | 値の出どころ | 形式 | 例 |
|---|---|---|---|---|
| `caption_models` | キャプションを作ったモデル | metadata.jsonl：`provenance.caption_model` の種類。2種類以上なら件数の多い順に件数を付ける | `名前` または `名前 (件数), 名前 (件数)` | `qwen3-vl:8b-instruct` |
| `verification_sample_size` | 目視で判定した行数 | metadata.jsonl：`verification.human_verified` が true の行数。抽出の記録（300、全体が300未満なら全行数）と一致することを確かめる | 件数 | `300` |
| `verification_seed` | 抽出に使ったシード | 検証係の抽出の記録 | 整数 | `20261115` |
| `verification_completed_date` | 判定を終えた日 | 検証係の記録 | 日付 | `2026-11-20` |
| `correct_count` | 「正しい」の行数 | metadata.jsonl：`verification.judgment` が `correct` | 件数 | `255` |
| `correct_pct` | 「正しい」の割合 | `correct_count` ÷ `verification_sample_size` × 100 | 割合 | `85.0` |
| `minor_error_count` | 「軽微な誤り」の行数 | `judgment` が `minor_error` | 件数 | `33` |
| `minor_error_pct` | 「軽微な誤り」の割合 | ÷ `verification_sample_size` × 100 | 割合 | `11.0` |
| `major_error_count` | 「重大な誤り」の行数 | `judgment` が `major_error` | 件数 | `12` |
| `major_error_pct` | 「重大な誤り」の割合 | ÷ `verification_sample_size` × 100 | 割合 | `4.0` |
| `major_error_ci95_low` | 重大な誤りの割合の95%信頼区間の下限 | Wilson のスコア法（下の式） | 割合 | `2.3` |
| `major_error_ci95_high` | 同じく上限 | Wilson のスコア法 | 割合 | `6.9` |
| `any_error_count` | 誤りあり（軽微＋重大）の行数 | `minor_error_count` + `major_error_count` | 件数 | `45` |
| `any_error_pct` | 誤りありの割合 | ÷ `verification_sample_size` × 100 | 割合 | `15.0` |
| `any_error_ci95_low` | 誤りありの割合の95%信頼区間の下限 | Wilson のスコア法 | 割合 | `11.4` |
| `any_error_ci95_high` | 同じく上限 | Wilson のスコア法 | 割合 | `19.5` |
| `field_error_ja_short` | `ja_short` に誤りがあった行数 | `verification.error_fields` に `ja_short` を含む行数 | 件数 | `14` |
| `field_error_ja_detailed` | `ja_detailed` に誤りがあった行数 | 同上 `ja_detailed` | 件数 | `31` |
| `field_error_en_short` | `en_short` に誤りがあった行数 | 同上 `en_short` | 件数 | `9` |
| `field_error_category` | `category` に誤りがあった行数 | 同上 `category` | 件数 | `8` |
| `verification_rounds` | データシートの数字が何回目の抽出・判定によるものか | 検証係の記録 | 整数 | `1` |
| `review_removed_count` | 抽出した行の確認中に、人物・ナンバー・ロゴなどが見つかって出荷から外した行数 | 検証係の抽出・判定の記録 | 件数 | `2` |

95%信頼区間（Wilson のスコア法）の式。k = 該当する行数、n = `verification_sample_size`、p = k ÷ n、z = 1.959964 として、

- 中心 = (p + z² ÷ (2n)) ÷ (1 + z² ÷ n)
- 幅 = z × √(p(1 − p) ÷ n + z² ÷ (4n²)) ÷ (1 + z² ÷ n)
- 下限 = 中心 − 幅、上限 = 中心 + 幅。0 未満は 0、1 を超えるものは 1 にしてから、それぞれ100倍して小数第1位に四捨五入する（`-0.0` を出さない）

有限母集団修正はしません（データシートにもそう書いてあります）。上の例（k = 12、n = 300）では 2.3〜6.9 になります。

`verification_sample_size` が `row_count` と同じ（全件確認）なら区間はありません。区間の4つのプレースホルダ（`major_error_ci95_low`・`major_error_ci95_high`・`any_error_ci95_low`・`any_error_ci95_high`）は使わず、4.3 の表の区間の2つのセルを `—` にし、表の下の区間の説明文を「全件を確認したため区間はありません」（英語版は "All rows were checked, so no interval is given."）に置き換えます。

### 分類（梱包係）

| プレースホルダ | 意味 | 値の出どころ | 形式 | 例 |
|---|---|---|---|---|
| `category_landscape_count` / `category_landscape_pct` | `landscape` の行数 / 割合 | metadata.jsonl：`category` ごとに数え、割合は ÷ `row_count` × 100 | 件数 / 割合 | `248` / `24.8` |
| `category_cityscape_count` / `category_cityscape_pct` | `cityscape` の行数 / 割合 | 同上 | 件数 / 割合 | `131` / `13.1` |
| `category_architecture_count` / `category_architecture_pct` | `architecture` の行数 / 割合 | 同上 | 件数 / 割合 | `162` / `16.2` |
| `category_railway_count` / `category_railway_pct` | `railway` の行数 / 割合 | 同上 | 件数 / 割合 | `96` / `9.6` |
| `category_transport_count` / `category_transport_pct` | `transport` の行数 / 割合 | 同上 | 件数 / 割合 | `38` / `3.8` |
| `category_food_count` / `category_food_pct` | `food` の行数 / 割合 | 同上 | 件数 / 割合 | `157` / `15.7` |
| `category_flora_count` / `category_flora_pct` | `flora` の行数 / 割合 | 同上 | 件数 / 割合 | `89` / `8.9` |
| `category_interior_count` / `category_interior_pct` | `interior` の行数 / 割合 | 同上 | 件数 / 割合 | `41` / `4.1` |
| `category_other_count` / `category_other_pct` | `other` の行数 / 割合 | 同上 | 件数 / 割合 | `38` / `3.8` |
| `category_rule_count` | 分類をルールで決めた・直した行数 | metadata.jsonl：`provenance.category_source` が `rule` | 件数 | `57` |

件数が0の分類も `0` / `0.0` と書きます（行を消さない）。

### 地域と時期（梱包係）

| プレースホルダ | 意味 | 値の出どころ | 形式 | 例 |
|---|---|---|---|---|
| `prefecture_count` | 写真のある都道府県の数 | metadata.jsonl：`location.prefecture` の種類の数（null を除く） | 整数 | `23` |
| `municipality_count` | 写真のある市区町村の数 | `location.municipality_code` の種類の数（null を除く） | 整数 | `141` |
| `top_prefectures` | 行数の多い都道府県（上位5） | `location.prefecture` ごとに数え、多い順。同数なら都道府県コード（`municipality_code` の上2桁）の小さい順。5つ未満ならあるだけ | `日本語名 英語名 件数` をカンマ区切り | `長野県 Nagano 182, 京都府 Kyoto 140, 北海道 Hokkaido 121, 東京都 Tokyo 88, 静岡県 Shizuoka 64` |
| `top5_prefecture_share_pct` | 上位5都道府県の行数の合計の割合 | 上の件数の合計 ÷ `row_count` × 100 | 割合 | `59.5` |
| `location_null_count` | `location` が null の行数 | metadata.jsonl | 件数 | `30` |
| `capture_first_year_month` | 最も古い撮影年月 | `capture.year_month` の最小（null を除く） | 年月 | `2019-04` |
| `capture_last_year_month` | 最も新しい撮影年月 | `capture.year_month` の最大 | 年月 | `2026-08` |
| `capture_null_count` | `capture` が null の行数 | metadata.jsonl | 件数 | `15` |
| `season_spring_count` | `capture.season` が `spring` の行数 | metadata.jsonl | 件数 | `231` |
| `season_summer_count` | `summer` の行数 | 同上 | 件数 | `198` |
| `season_autumn_count` | `autumn` の行数 | 同上 | 件数 | `312` |
| `season_winter_count` | `winter` の行数 | 同上 | 件数 | `214` |
| `season_unknown_count` | 季節が分からない行数 | `capture` が null、または `capture.season` が null の行数。季節の5つの合計が `row_count` になることを確かめる | 件数 | `45` |

### 作成の工程（梱包係・generation_summary.json）

| プレースホルダ | 意味 | 値の出どころ | 形式 | 例 |
|---|---|---|---|---|
| `source_photo_count` | 選別に入れた写真の枚数 | 手入力（副業Aの選別係の集計。分からなければ `-`） | 件数 | `9,812` |
| `selected_photo_count` | 選別後の枚数 | 手入力（副業Aの選別係の集計。分からなければ `-`） | 件数 | `7,140` |
| `rights_ok_count` | 権利チェックで「可」の枚数 | 副業Aの権利チェック係の集計（本人の最終確認後） | 件数 | `6,512` |
| `subset_seed` | この版の候補を無作為に選んだときのシード | 抽出の記録。3.1 の［選択］で無作為を選んだときだけ使う | 整数 | `20261020` |
| `caption_runtime` | モデルを動かしたソフトとそのバージョン | `generation_summary.json`（`ollama -v` など） | `名前 バージョン` | `Ollama 0.40.0` |
| `prompt_versions` | 使ったプロンプトの版 | metadata.jsonl：`provenance.prompt_version` の種類。2種類以上なら件数の多い順に件数を付ける | `caption-v1` または `caption-v1 (912), caption-v2 (88)` | `caption-v1` |
| `caption_generation_settings` | 生成の設定 | `generation_summary.json` | `キー=値` をカンマ区切り | `temperature=0, seed=42; retry: temperature=0.3, seed=43` |
| `caption_generated_start` | 出荷するキャプションを生成した最初の日 | `generation_summary.json` | 日付 | `2026-11-02` |
| `caption_generated_end` | 同じく最後の日 | `generation_summary.json` | 日付 | `2026-11-09` |
| `caption_input_long_edge_px` | モデルに渡した縮小画像の長辺 | `generation_summary.json`（パイロットで決めた値） | 整数（カンマなし） | `1280` |
| `caption_failure_excluded_count` | seed を変えて再試行しても検査に通らず、収録しなかった写真の枚数 | `generation_summary.json`（梱包の検査でキャプションの書式のエラーで外した行があれば足す） | 件数 | `6` |
| `vlm_flagged_count` | モデルが flags を立てて除外した写真の枚数 | `generation_summary.json`（この版の候補のうち） | 件数 | `41` |
| `regenerated_categories` | この版で作り直した分類 | `generation_summary.json`。分類のコードをカンマ区切り。なければ `-` | コードの並び | `food, interior` |
| `boundary_data_credit` | 行政区域データの出典表記 | 手入力。データの利用約款が求める書き方に合わせる（要確認）。公開前に、国土数値情報の利用約款と N03 のページで、出典の書き方と商用利用の条件を本人が確かめる | 文字列 | `「国土数値情報（行政区域データ）」（国土交通省）を加工して作成` |
| `boundary_data_version` | 行政区域データの版 | 手入力。ダウンロードしたデータの表記（基準日を含むもの）に合わせる | 文字列 | `N03-20260101` |

### 配布（梱包係）

| プレースホルダ | 意味 | 値の出どころ | 形式 | 例 |
|---|---|---|---|---|
| `package_dir` | 納品フォルダの名前 | 梱包係 | 文字列 | `jts-1.0.0-2048` |
| `id_prefix` | `id` の前半（ハイフンの前） | metadata.jsonl の `id`（全行で同じであることを確かめる） | 英小文字と数字 | `jts` |
| `metadata_sha256` | metadata.jsonl の SHA-256 | 梱包係：検証の後の最終の metadata.jsonl から計算（`shasum -a 256 metadata.jsonl`） | 小文字の16進64桁 | `f3485775238d36bbc09f686407a3d1ff7dbff6e5d7a52a6411cbb56061879fc0` |
| `package_size_gb` | 納品フォルダ全体の大きさ | 梱包係：検証の後の最終の納品フォルダのバイト数 ÷ 10⁹ | 小数第1位 | `1.2` |

## 選択箇所

プレースホルダのほかに、手で選ぶ箇所が1つあります。3.1 の4番目「この版の候補」です。日本語版は `［選択：…］`、英語版は `[CHOOSE: …]` で始まります。どれか1つの文を残し、ほかの文と角かっこの部分を消してください。両方の版で同じものを選びます。

## 公開前に本人が決めること（プライバシー）

各行の市区町村と撮影年月、撮影者の名前を合わせると、名前の分かる1人の旅行の履歴になります。次の3つは、係が決めずに本人が決めます。

- `licensor_name` は契約書と同じ表記が必要だが、公開するデータシートでは屋号や筆名でもよいか決める（著作権者の表記は正確に保つ）。
- 市区町村の上位を見て、自宅のある市区町村の行や自宅の写真（特に `interior`）を除くか決める。
- `contact_email` は個人のアドレスではなく、このデータセット用の窓口にする。

## 記入後の確認

`dataset-b/` で実行します。埋めたファイルの名前に合わせてください。

```sh
# 何も出なければよい（プレースホルダや選択箇所が残っていない）
grep -nE '\{\{|［選択|\[CHOOSE' DATASHEET.ja.md DATASHEET.en.md
```

- 冒頭の「AI生成キャプション」の数値が、4.3 の表と一致している。
- 季節の5つの合計、分類の件数の合計が、`row_count` と一致している。
- `correct_count` + `any_error_count` が `verification_sample_size` と一致している。

## テンプレートの保守メモ

- 2つのテンプレートのプレースホルダは、同じ集合に保ちます。変えたら次で確かめます（`dataset-b/` で実行）。

  ```sh
  python3 -c "import re,pathlib as p;f=lambda n:set(re.findall(r'\{\{([a-z0-9_]+)\}\}',p.Path('datasheet/DATASHEET.'+n+'.template.md').read_text(encoding='utf-8')));a,b=f('ja'),f('en');print(len(a),len(b),sorted(a^b))"
  ```

  2つの数が同じで、最後が `[]` ならそろっています。
- 冒頭の開示文は、計画書の文をもとにしています。言い回しを変えるときは、日英の両方を直します。
- 2.1 の項目の表は `schema/record.v1.schema.json` と1対1です。スキーマの項目を増やしたり名前を変えたりしたら、日英の両方の表を直します。
- 9 の読み込み例は `load_dataset("imagefolder", ...)` を使います。レコードの項目を `image_info` にしてあるのは、ImageFolder の画像の列 `image` と名前がぶつからないようにするためです。

## 参考にした枠組み

| データシートの節 | Datasheets for Datasets の問い | Hugging Face データセットカードの節 |
|---|---|---|
| 1 概要 | Motivation | Dataset Description |
| 2 収録内容 | Composition | Dataset Structure |
| 3 作成方法 | Collection Process、Preprocessing/cleaning/labeling | Dataset Creation（Source Data、Annotations） |
| 4 品質と誤り率 | Composition（errors, noise） | Annotations、Bias, Risks, and Limitations |
| 5 除外とプライバシー | Composition（identify individuals, sensitive data） | Personal and Sensitive Information |
| 6 利用条件の要約 | Distribution（license, terms of use） | YAML の license |
| 7 想定する用途 / 向かない用途 | Uses | Uses（Direct Use、Out-of-Scope Use） |
| 8 既知の限界と偏り | Uses（composition that might impact future uses） | Bias, Risks, and Limitations |
| 9 配布形式 | Distribution | Dataset Structure |
| 10 問い合わせ・削除依頼、11 改訂履歴 | Maintenance | Dataset Card Contact |

- Datasheets for Datasets（Gebru ほか、arXiv 1803.09010）。問いの一覧は、mlr3fairness パッケージの雛形（論文 v7 の問い）で確かめました：https://github.com/mlr-org/mlr3fairness/blob/main/inst/rmarkdown/templates/datasheets/skeleton/skeleton.Rmd
- Hugging Face データセットカードの雛形：https://github.com/huggingface/huggingface_hub/blob/main/src/huggingface_hub/templates/datasetcard_template.md
- Hugging Face ImageFolder と metadata.jsonl：https://github.com/huggingface/datasets/blob/main/docs/source/image_dataset.mdx
