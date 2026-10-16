# {{dataset_name}} データシート（版 {{dataset_version}}）

> **AI生成キャプション**
>
> キャプションはローカルの画像言語モデル（{{caption_models}}）で自動生成したもので、人による全件確認はしていません。無作為に抽出した{{verification_sample_size}}件を目視で確認した結果、重大な誤りは{{major_error_count}}件（{{major_error_pct}}%）、軽微な誤りは{{minor_error_count}}件（{{minor_error_pct}}%）でした。場所（市区町村まで）と撮影時期は、写真の位置情報と撮影日時から機械的に付けています。人物・車のナンバー・ロゴが写った写真は除外しています。
>
> 除外は自動検出と目視確認によるもので、完全ではありません。見落としがあれば、申し出を受けて削除します（→ 10）。写真は撮影者が撮ったもので、生成AIによる生成・合成・加工はしていません（スマートフォン標準の画像処理を除く）。AIが作ったのはキャプションと分類だけです。

## 1. 概要

| 項目 | 内容 |
|---|---|
| 名前 | {{dataset_name}} |
| 版・公開日 | {{dataset_version}}（{{release_date}}） |
| 内容 | 旅行写真（人物の写った写真は除外。場所が分かるものはすべて日本国内）と、日本語・英語のキャプション |
| 件数 | {{row_count}} 件（画像1枚 = 1行） |
| 画像 | JPEG。この版の長辺は {{image_long_edge_min_px}}〜{{image_long_edge_max_px}} px。納品は「長辺2,048pxに縮小」か「原寸」を購入者ごとに選びます。EXIF・XMP・位置情報などのメタデータは削除済み（sRGB の色プロファイルだけ残す） |
| キャプション | 日本語の短文と詳細文、英語の短文（日本語短文の英訳）。AI生成 |
| そのほかの項目 | 分類（9種）、場所（都道府県・市区町村）、撮影年月と季節、生成の記録、人による検証の結果 |
| 撮影者・著作権者 | {{licensor_name}} |
| 利用条件 | （要約）非独占。機械学習の学習・評価にのみ利用可。再配布不可。画像をストック素材・展示用に使うことは不可（→ 6） |
| 連絡先 | {{contact_email}} |

## 2. 収録内容

### 2.1 1行の項目（metadata.jsonl）

「付け方」の意味：**梱包** = 納品データを作るときに計算・採番、**AI** = ローカル画像言語モデルの出力、**メタデータ** = 元写真の位置情報・撮影日時から機械的に決定（AIは使わない）、**ルール** = 決まった手順で機械的に決定、**記録** = 生成の工程で記録、**人** = 撮影者本人の目視判定。

| 項目 | 型 | 内容 | 付け方 |
|---|---|---|---|
| `id` | string | 行のID（例 `{{id_prefix}}-000001`）。元のファイル名や撮影日とは無関係 | 梱包 |
| `file_name` | string | 画像のパス。常に `images/` + `id` + `.jpg` | 梱包 |
| `image_info.width` | integer | 納品画像の幅（px） | 梱包 |
| `image_info.height` | integer | 納品画像の高さ（px） | 梱包 |
| `image_info.sha256` | string | 納品画像ファイルの SHA-256（小文字の16進64桁） | 梱包 |
| `captions.ja_short` | string | 日本語の短文。1文・常体・15〜60字。主な被写体とその様子 | AI |
| `captions.ja_detailed` | string | 日本語の詳細文。3〜5文・常体・計80〜300字。構図、季節や時間帯の手がかり（見える場合だけ）、光、色、雰囲気 | AI |
| `captions.en_short` | string | 英語の短文。`ja_short` の英訳で、内容は足さない。1文・5〜30語 | AI |
| `category` | string | 主な被写体の分類（→ 2.2） | AI（一部ルール） |
| `location` | object または null | 撮影地点の市区町村。位置情報がない写真と、国内の行政区域に当たらなかった写真（国外・海上など）は null | メタデータ |
| `location.country` | string | 常に `"JP"` | ルール |
| `location.prefecture` | string | 都道府県名（例 `長野県`） | メタデータ |
| `location.prefecture_en` | string | 都道府県名の英語表記（例 `Nagano`）。47件の固定の対応表による | ルール |
| `location.municipality` | string | 市区町村名（例 `松本市`、`横浜市中区`、`千代田区`）。政令指定都市は区まで | メタデータ |
| `location.municipality_code` | string | 全国地方公共団体コード5桁（検査数字なし）。上2桁は都道府県コード | メタデータ |
| `capture` | object または null | 撮影時期。撮影日時のない写真は null | メタデータ |
| `capture.year_month` | string | 撮影年月（`YYYY-MM`） | メタデータ |
| `capture.season` | string または null | `spring`（3〜5月）・`summer`（6〜8月）・`autumn`（9〜11月）・`winter`（12〜2月）。気象庁の区分で撮影月から決める。`location` が null なら null（南北どちらの半球か分からないため） | ルール |
| `provenance.caption_source` | string | 常に `"ai_generated"` | ルール |
| `provenance.caption_model` | string | キャプションを作ったモデルの名前とタグ（→ 3.2） | 記録 |
| `provenance.prompt_version` | string | プロンプトの版（例 `caption-v1`） | 記録 |
| `provenance.category_source` | string | `model`（モデルが選んだ）か `rule`（ルールで決めた・直した） | 記録 |
| `verification.human_verified` | boolean | 人が画像と照らして確認した行なら true | 人 |
| `verification.judgment` | string または null | `correct`・`minor_error`・`major_error`（→ 4.1）。確認していない行は null | 人 |
| `verification.error_fields` | array（string） | 誤りがあった項目（`ja_short`・`ja_detailed`・`en_short`・`category`）。誤りがなければ空 | 人 |

座標、住所、正確な撮影日時、元のファイル名、カメラ情報の項目はありません。形式の定義は `schema/record.v1.schema.json`（JSON Schema draft 2020-12）にあります。

### 2.2 分類（category）

主な被写体で1つだけ選びます。

| コード | ラベル | 主な被写体 | 件数 | 割合 |
|---|---|---|---|---|
| `landscape` | 風景 | 山・海・川・湖・田畑・空などの自然の景色（広く写したもの） | {{category_landscape_count}} | {{category_landscape_pct}}% |
| `cityscape` | 街並み | 通り・路地・町並み・町の夜景、複数の建物 | {{category_cityscape_count}} | {{category_cityscape_pct}}% |
| `architecture` | 建物 | 1つの建物や構造物が中心（寺・神社・城・橋・塔・家） | {{category_architecture_count}} | {{category_architecture_pct}}% |
| `railway` | 鉄道 | 列車・駅・線路が中心 | {{category_railway_count}} | {{category_railway_pct}}% |
| `transport` | 乗り物 | 鉄道以外の乗り物（船・フェリー・バス・飛行機・ロープウェイ） | {{category_transport_count}} | {{category_transport_pct}}% |
| `food` | 食 | 料理・食材・飲み物・菓子 | {{category_food_count}} | {{category_food_pct}}% |
| `flora` | 植物・花 | 花・木・紅葉を近くか中くらいの距離で写したもの | {{category_flora_count}} | {{category_flora_pct}}% |
| `interior` | 屋内 | 空間そのものが被写体の屋内（宿の部屋・店内） | {{category_interior_count}} | {{category_interior_pct}}% |
| `other` | その他 | 上のどれにも当たらないもの | {{category_other_count}} | {{category_other_pct}}% |

迷ったときの決め方：目立つ列車があれば、風景の中でも `railway`。室内の卓上の料理は `food`。木々の中の鳥居1基は `architecture`。紅葉の山を広く写したものは `landscape`（`flora` ではない）。

分類はモデルが選び、うち {{category_rule_count}} 件はルールで決めた・直したものです（`provenance.category_source` が `"rule"`）。

### 2.3 地域と時期

- 都道府県：{{prefecture_count}}（47のうち）。市区町村：{{municipality_count}}
- 件数の多い都道府県（上位5）：{{top_prefectures}}。上位5で全体の {{top5_prefecture_share_pct}}%
- 場所なし（`location` が null）：{{location_null_count}} 件（国外で撮った写真が含まれることがあります）
- 撮影期間：{{capture_first_year_month}} 〜 {{capture_last_year_month}}。撮影時期なし（`capture` が null）：{{capture_null_count}} 件

| 春 `spring` | 夏 `summer` | 秋 `autumn` | 冬 `winter` | 不明（null） |
|---|---|---|---|---|
| {{season_spring_count}} | {{season_summer_count}} | {{season_autumn_count}} | {{season_winter_count}} | {{season_unknown_count}} |

都道府県別・市区町村別の全件数は、metadata.jsonl から数えられます。

## 3. 作成方法

### 3.1 写真の選び方

1. **撮影**：{{licensor_name}} が旅行中に撮った写真です。主に iPhone で撮っています。
2. **選別**：HEIC を JPEG に変換し、連写の重複を除き、ブレと露出を点数にして選びました。
3. **権利チェック**：顔・人物と文字（看板・ナンバープレート・ロゴ）を自動で検出して「除外／要確認／可」に分け、最後に撮影者本人が目視で確認しました。使うのは「可」の写真だけです（→ 5）。
4. **この版の候補**：［選択：次のどれか1つを残し、ほかと角かっこを消す］「可」の写真をすべて使いました。／「可」の写真から無作為に選びました（シード {{subset_seed}}）。／（選び方を書く）
5. **キャプション生成と保留**：3.2 の検査と確認を経て、{{row_count}} 件になりました。

| 段階 | 枚数 |
|---|---|
| 選別に入れた写真 | {{source_photo_count}} |
| 選別後 | {{selected_photo_count}} |
| 権利チェックで「可」 | {{rights_ok_count}} |
| この版に収録 | {{row_count}} |

### 3.2 キャプションと分類の生成

- **モデル**：{{caption_models}}。撮影者の Mac 上で動かしました（{{caption_runtime}}）。クラウドのAPIは使っていません。
- **プロンプト**：{{prompt_versions}}。設定は {{caption_generation_settings}}。
- **生成期間**：{{caption_generated_start}} 〜 {{caption_generated_end}}
- **モデルへの入力**：向きを補正し、メタデータを消した縮小画像（長辺 {{caption_input_long_edge_px}} px）だけです。場所や撮影日は渡していません。地名などの固有名詞が入ることと、写っていない季節を推測することを避けるためです。
- **書き方の指示**：見えるものだけを書く。固有名詞（地名・寺社や施設の名前・店やブランドの名前・路線名や車両の形式名・山の名前）を書かない。人を描写しない。季節と時間帯は、見える手がかり（紅葉・桜・雪・夕焼け・夜景など）があるときだけ書く。数は、はっきり数えられる5以下のものだけ。
- **機械的な検査**：出力の形（JSON Schema）と、文の数・文字数・語数・文末などのルールを機械的に検査しました。seed を変えて再試行しても通らなかった写真 {{caption_failure_excluded_count}} 枚は収録していません。内容の正しさは機械では確かめていません（→ 4）。
- **第2の確認**：モデルが人物・ナンバープレート・ロゴやブランド名を見つけた写真 {{vlm_flagged_count}} 枚は、誤検出の可能性があっても収録していません。
- **作り直し**：重大な誤りが多かった分類は、プロンプトかモデルを変えて作り直し、検証もやり直します。この版で作り直した分類：{{regenerated_categories}}

### 3.3 場所と撮影時期の付け方

- **場所**：元写真の位置情報（GPS）を行政区域のデータと照らし合わせ、オフラインで都道府県と市区町村を決めました。AIは使っていません。市区町村より細かい情報は収録していません。行政区域のデータ：{{boundary_data_credit}}（版 {{boundary_data_version}}）
- **撮影時期**：元写真の撮影日時（EXIF）から年と月だけを取りました。季節は撮影月から機械的に決めたもので、画像の見た目からではありません。
- この2つは誤り率の検証の対象外です（限界は → 8）。

### 3.4 納品画像の処理

1. EXIF の向きの情報を画素に反映する。
2. 埋め込みの色プロファイル（iPhone では多くが Display P3）から sRGB に変換する。
3. EXIF・XMP・IPTC・位置情報・コメントを削除する（残すのは JPEG の基本ヘッダーと sRGB の色プロファイルだけ）。
4. 長辺2,048pxに縮小する（原寸版は縮小しない）。
5. JPEG で保存し、ファイルの SHA-256 を `image_info.sha256` に記録する。

原寸版も元のファイルそのものではなく、JPEG に変換してメタデータを消したものです。

## 4. 品質と誤り率

### 4.1 判定の基準

| 判定 | 基準 |
|---|---|
| 正しい（`correct`） | 書かれていることがすべて画像に合っている。書き漏れは誤りとしない |
| 軽微な誤り（`minor_error`） | 主な被写体と分類は正しいが、副次的な細部（色・数・小さな要素の位置）が違う。日本語がぎこちないが意味は通じる。`en_short` と `ja_short` の違いがニュアンスだけ |
| 重大な誤り（`major_error`） | 主な被写体の取り違え。目立つものを、写っていないのに書いている。分類の誤り。画像と矛盾する季節・時間帯の断定。地名・施設名・ブランド名などの固有名詞。人の描写。`en_short` の意味が `ja_short` と大きく違う。誤解を招くほど崩れた文 |

1行の判定は、`ja_short`・`ja_detailed`・`en_short`・`category` のうち最も悪いものです。誤りのあった項目は `verification.error_fields` に入っています。

### 4.2 抽出の方法

- **対象**：この版で出荷するキャプション（{{row_count}} 行）
- **方法**：単純無作為抽出（非復元）で {{verification_sample_size}} 行を選びました（目標は300行。全体が300行未満のときは全行）。シード {{verification_seed}}
- **判定者**：撮影者本人が画像を見て判定しました。判定にAIは使っていません。判定の完了日：{{verification_completed_date}}
- 判定の後にキャプションを作り直した場合は、抽出と判定をやり直しています。下の数値は、出荷するキャプションについての {{verification_rounds}} 回目の抽出によるものです（前の回の結果は記録に残しています）。

### 4.3 結果

| 判定 | 件数 | 割合 | 95%信頼区間 |
|---|---|---|---|
| 正しい | {{correct_count}} | {{correct_pct}}% | — |
| 軽微な誤り | {{minor_error_count}} | {{minor_error_pct}}% | — |
| 重大な誤り | {{major_error_count}} | {{major_error_pct}}% | {{major_error_ci95_low}}〜{{major_error_ci95_high}}% |
| 誤りあり（軽微＋重大） | {{any_error_count}} | {{any_error_pct}}% | {{any_error_ci95_low}}〜{{any_error_ci95_high}}% |

95%信頼区間は Wilson のスコア法で計算しました。有限母集団修正はしていないので、区間は広め（保守的）です。

| 誤りがあった項目 | 行数 |
|---|---|
| `ja_short` | {{field_error_ja_short}} |
| `ja_detailed` | {{field_error_ja_detailed}} |
| `en_short` | {{field_error_en_short}} |
| `category` | {{field_error_category}} |

1行で複数の項目に誤りがあることがあるので、合計は誤りありの行数（{{any_error_count}}）より多くなることがあります。

### 4.4 使うときの注意

- 判定した行のキャプションも、生成したままです（手で直していません）。`verification.judgment` で絞り込めます（例：`"correct"` の行だけを使う、`"major_error"` の行を除く）。
- 誤り率は抽出した行からの推定です。確認していない個々の行の正しさは保証しません。
- 分類ごとの内訳は、metadata.jsonl の `category` と `verification` から計算できます。
- 場所と撮影時期は、メタデータから機械的に付けたもので、この誤り率には含みません。

## 5. 除外とプライバシー

- **除外したもの**：権利チェック（自動検出と撮影者本人の目視）で、顔・人物、車のナンバープレート、主な被写体としてのロゴ・ブランドマーク、撮影が制限された屋内を除き、「可」になった写真だけを使っています。さらに、キャプションを作るモデルが人物・ナンバー・ロゴやブランド名を見つけた写真も、すべて除外しました（3.2）。
- **限界**：自動検出も目視も完全ではありません。小さく写った人や、読めるナンバー・文字が残っている可能性があります。見つけた場合は連絡してください。確認のうえ、その行を削除します（→ 10）。抽出した行を確認する途中で、人物・ナンバー・ロゴなどが写った写真が {{review_removed_count}} 件見つかり、除外しました。確認していない行にも、同じくらいの割合で残っている可能性があります。
- **メタデータ**：納品画像からは、EXIF・XMP・位置情報などのメタデータを削除しています（sRGB の色プロファイルだけ残します）。
- **丸め**：場所は市区町村まで、撮影時期は年と月までです。座標・住所・正確な日時・元のファイル名・カメラ情報は含みません。
- **キャプション**：人の描写と固有名詞を書かないように指示しています。ただし全件は確認していません（見つかれば「重大な誤り」として数えています）。

## 6. 利用条件の要約

これは要約で、法的な助言ではありません。拘束力があるのは、契約書またはマーケットプレイスのライセンス（{{license_reference}}）の本文です。

- 非独占のライセンスです。同じデータをほかの購入者にも提供することがあります。
- 用途は、機械学習モデルの学習・ファインチューニング・評価に限ります。
- 画像とキャプションの再配布・転売・サブライセンスはできません。
- 画像をストック素材・報道・展示など、見せるための素材として公開・利用することはできません。
- 写っている場所の正確な位置や、人物を特定しようとしないでください。
- 写真の著作権は撮影者（{{licensor_name}}）に残ります。

## 7. 想定する用途 / 向かない用途

**想定する用途**

- 日本語・英語の画像キャプション生成の学習と評価
- 画像とテキストの対応付け・検索の学習
- 日本の風景・街並み・建物・食などの場面分類（`category`）
- 日本語に対応した画像言語モデルのファインチューニング

**向かない用途**

- 確認していない行を、そのまま評価の正解として使うこと（評価の正解には、`verification.judgment` が `"correct"` の行が向いています）
- 人物に関するタスク（人物の写った写真は除外対象です）
- 地名や施設名の認識（キャプションには固有名詞を書かないよう指示しています）
- 細かい位置の推定（場所は市区町村までで、撮影地点のものです）
- キャプションの季節・時間帯を正解ラベルとして扱うこと（見た目からの記述で、検証は抽出分だけです）
- 英語の詳しい説明文の学習（英語は短文の翻訳だけです）
- 日本全体を代表するデータとして扱うこと（→ 8）
- 画像を見せるための利用（利用条件で禁止。→ 6）

## 8. 既知の限界と偏り

- **撮影者が1人**：題材・構図・時間帯に、1人の好みが出ています。
- **旅行の写真**：観光地、移動中の景色、食事が中心です。日常の生活の場面は少なめです。
- **人のいない場面への偏り**：人物・ナンバー・ロゴの写った写真を除いたため、混雑した場所や街中の場面は少なめです。キャプションを作れなかった写真の除外（3.2）も、偏りを生んでいる可能性があります。
- **地域**：{{prefecture_count}} 都道府県（47のうち）。上位5都道府県で全体の {{top5_prefecture_share_pct}}% です（→ 2.3）。
- **季節と年**：季節ごとの件数は 2.3 の表のとおりです。撮影期間は {{capture_first_year_month}} 〜 {{capture_last_year_month}} です。
- **機材**：主に iPhone です。HDR合成やノイズ処理など、スマートフォン特有の画像処理の質感と、広角寄りの画角が多くなっています。
- **AIキャプションの誤り**：写っていないものを書く、色・数・位置を誤る、見えない季節や時間帯を推測する、といった誤りがあります（→ 4.3）。言い回しが似通うことがあります。
- **英語**：`en_short` は `ja_short` の英訳です。英語で独立に書いたものではなく、詳細文の英語はありません。
- **場所**：撮影地点（カメラの位置）の市区町村です。写っているもの（遠くの山など）の所在地とは限りません。境界の近くでは、GPS の誤差で隣の市区町村になることがあります。行政区域のデータは版 {{boundary_data_version}} のもので、その後の市町村合併などは反映していません。
- **撮影時期**：カメラの時計によります。季節は撮影月で決めたもので、見た目と合わないことがあります（例：3月の雪景色は `spring`）。
- **似た写真**：連写の重複は除いていますが、同じ場所・同じ日の似た写真は残っています。学習用と評価用に分けるときは、`location.municipality_code` や `capture.year_month` の単位で分けることを勧めます。
- **ほかでの公開**：一部の写真は、撮影者がストックフォトとして別に販売しています（または今後販売します）。同じ画像がウェブ上のほかのデータに含まれている可能性があります。評価に使うときは注意してください。

## 9. 配布形式

```text
{{package_dir}}/
├── metadata.jsonl                 1行 = 画像1枚（{{row_count}} 行、UTF-8）
├── images/
│   ├── {{id_prefix}}-000001.jpg
│   └── ...
├── schema/
│   └── record.v1.schema.json      metadata.jsonl の1行の形式（JSON Schema draft 2020-12）
├── DATASHEET.ja.md
└── DATASHEET.en.md
```

- 全体の大きさ：{{package_size_gb}} GB
- metadata.jsonl の SHA-256：`{{metadata_sha256}}`
- 画像ファイルの SHA-256 は、各行の `image_info.sha256` にあります。

Hugging Face `datasets` での読み込み例：

```python
from datasets import load_dataset

root = "{{package_dir}}"  # metadata.jsonl のあるフォルダ
ds = load_dataset("imagefolder", data_dir=root, split="train")  # 画像は image 列

# 例：人が確認して「正しい」と判定した行だけ（verification 列だけを見るので画像は読まない）
checked = ds.filter(lambda v: v["judgment"] == "correct", input_columns="verification")
```

## 10. 問い合わせ・削除依頼

- 連絡先：{{contact_email}}
- **削除依頼**：人・ナンバー・ロゴなどが写っている、権利に問題がある、といった行を見つけたら、`id` を添えて連絡してください。{{takedown_response_days}} 日以内に返信します。確認できた行は削除して改訂版を出し、購入者には削除した `id` を知らせます。購入者には、該当する画像とキャプションの削除をお願いします。
- **誤りの報告**：キャプション・分類・場所の誤りも、`id` を添えて知らせてください。次の版で見直します。

## 11. 改訂履歴

| 版 | 日付 | 内容 |
|---|---|---|
| {{dataset_version}} | {{release_date}} | 初版 |

---

このデータシートの構成は、Datasheets for Datasets（Gebru ほか）と Hugging Face のデータセットカードを参考にしています。
