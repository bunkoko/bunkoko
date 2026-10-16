# 副業B：学習用データセット（仕組みと雛形）

人の写っていない旅行写真に、日本語・英語のキャプションを付けた AI 学習用データセットを作るための、プロンプト・スキーマ・データシート雛形・検査ツールです。計画書「副業B：学習用データセット計画」の最初のタスク（キャプション生成のプロンプト、JSONLのスキーマ、データシートの雛形）にあたります。

このリポジトリは公開されています。写真、元のファイル名、生成ログ、判定結果、納品フォルダはリポジトリの外の作業フォルダ（例：`~/bunkoko-work/`）に置き、コミットしません（`.gitignore` でも画像と `metadata.jsonl` を除外しています）。

## ファイル

| ファイル | 中身 |
|---|---|
| `prompts/caption_v1_system.txt`・`caption_v1_user.txt` | ローカルの画像言語モデル（VLM）に渡すプロンプト（caption-v1） |
| `prompts/README.md` | プロンプトの設計の理由、Ollama での呼び出し方、モデル選びのパイロット手順、版の管理 |
| `schema/caption_output.v1.schema.json` | VLM が返す JSON の形。Ollama の `format` にそのまま渡す |
| `schema/record.v1.schema.json` | 納品する `metadata.jsonl` の1行の形（JSON Schema draft 2020-12）。納品物にも同梱する |
| `tools/validate_records.py` | `metadata.jsonl` の検査（スキーマ、項目間の整合、キャプションの書式、`--images-root` で画像のメタデータ残り）。生成時の検査 `check_caption_output()` もここ |
| `tests/` | 検査ツールのテスト（標準の unittest） |
| `examples/metadata.sample.jsonl` | 説明用の架空の4行。販売先へのサンプルや納品物には使わない |
| `datasheet/DATASHEET.ja.template.md`・`DATASHEET.en.template.md` | 買い手向けデータシートの雛形（日英）。`{{名前}}` を埋めて使う |
| `datasheet/PLACEHOLDERS.md` | 雛形の83個の `{{名前}}` の意味・出どころ・書式、公開前に本人が決めること |
| `../.claude/agents/caption-writer.md` | キャプション係（sonnet） |
| `../.claude/agents/caption-verifier.md` | 検証係（opus、effort: high） |
| `../.claude/agents/dataset-packager.md` | 梱包係（haiku） |

副業Aの係（選別・権利チェック・場所）はそのまま使うので、ここでは定義していません。

## 流れ

1. **入力（副業A）**：権利チェックで「可」の写真、場所係の出力（都道府県・市区町村・5桁コード）、EXIF の撮影日時。
2. **キャプション係**：向きを直してメタデータを消した縮小コピーをローカル VLM に渡し、`caption_output` の形で受け取る。`check_caption_output()` で書式を確かめ、`flags`（人・ナンバー・ロゴ）が空の写真だけを梱包前レコードにする。場所と撮影時期は VLM ではなくメタデータから付ける。
3. **梱包係**：画像を処理（向きの反映、sRGB 化、メタデータ削除、長辺2,048px か原寸）し、`id` を振って `metadata.jsonl` を書く。`validate_records.py --images-root` がエラー0でなければ出さない。データシートの品質以外の欄を埋める。
4. **検証係**：出荷する版から300件を無作為に抜き、本人が iPad で「正しい／軽微な誤り／重大な誤り」を判定する。件数・%・Wilson 95%区間を集計し、データシートの品質の欄と開示文を埋める。
5. **梱包係**：検証結果を反映した `metadata.jsonl` をもう一度検査し、ハッシュと大きさを書き直して納品フォルダを仕上げる。

## 動かし方

`dataset-b/` で実行します。必要なのは Python 3.10 以上と jsonschema です（画像の検査には Pillow も）。

```sh
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests
python3 tools/validate_records.py examples/metadata.sample.jsonl
python3 tools/validate_records.py <納品フォルダ>/metadata.jsonl --images-root <納品フォルダ>
```

## 次にやること

計画書のスケジュール（3〜4週目にキャプション生成を足す）に合わせ、副業Aの処理ができてから進めます。

1. **モデル選びのパイロット**：Aで「可」になった写真50枚を2つの候補モデルで生成し、本人が伏せ字で判定する（手順は `prompts/README.md`）。
2. **生成スクリプト**（キャプション係）、**梱包スクリプト**（梱包係）、**抽出・確認ページ・集計**（検証係）を作る。各係の定義に、作るファイルと完了の条件を書いてあります。
3. **出品の前に本人が確かめること**
   - 販売先ごとに、拘束力のあるライセンスにデータシート 6 の6項目（非独占、学習・評価用途のみ、再配布不可、ストック・展示用の利用不可、人物や正確な場所の特定不可、著作権は撮影者）が入っているか（`PLACEHOLDERS.md` の `license_reference`）。
   - 特に Wirestock：二次情報によると、投稿した写真を Getty・Adobe Stock・Canva にも配り、AI企業との学習契約は Wirestock の条件で結ぶ。「ストック用途に使わせない」条件や、副業Aでの Adobe Stock への直接出品とぶつかる可能性があるので、規約を読んでから決める。
   - 公開する名前、自宅周辺の行を除くか、連絡先（`PLACEHOLDERS.md` の「公開前に本人が決めること」）。
   - 行政区域データ（国土数値情報）の出典表記と商用利用の条件。
