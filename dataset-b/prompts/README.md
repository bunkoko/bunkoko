# キャプション生成プロンプト（caption-v1）

副業B（学習用データセット）のキャプションを、Mac上のローカル画像言語モデル（VLM）で作るためのプロンプトと出力形式です。クラウドのAPIは使いません。

## ファイル

| ファイル | 中身 |
|---|---|
| `prompts/caption_v1_system.txt` | システムプロンプト。書き方のルール、分類の定義と迷ったときの決め方、注意フラグ、出力形式、形式の例 |
| `prompts/caption_v1_user.txt` | 写真1枚ごとに送る短い指示。画像は別に添付する。場所や日付は入れない |
| `schema/caption_output.v1.schema.json` | VLMが返すJSONの形。Ollamaの `format` にそのまま渡す |

VLMが返すのは `ja_short` / `ja_detailed` / `en_short` / `category` / `flags` の5つです。これに場所・撮影時期・出どころ・検証欄を足して1行のレコード（`schema/record.v1.schema.json`）にするのは後の工程です。このとき `provenance.prompt_version` は `"caption-v1"` にします。

## 設計の判断と理由

1. **場所と撮影日はVLMに渡さない。** キャプションは画像だけから作ります。地名を渡すと、キャプションに固有名詞が漏れます。撮影月を渡すと「12月だから雪」のように、見えない季節を書きやすくなります。場所と撮影時期は、位置情報と撮影日時から機械的に付ける別の欄で正確に持ちます。キャプションがメタデータから独立しているので、検証する人は画像だけを見て正誤を判定できます。
2. **固有名詞は禁止。** ローカルモデルの「場所当て」は外れやすく、間違った固有名詞は判定基準で重大な誤りになります。寺や店の名前を書くと、市区町村までに丸めた位置情報よりも細かく場所が分かってしまいます。看板の文字を書き写すと、店名やブランド名が入ります。「日本」「Japan」も地名なので書かせません。
3. **flags は第2の安全網。** 使う写真は、副業Aの権利チェックで「可」になったものだけです。ただ、検出は完全ではありません。VLMが人・ナンバープレート・ロゴを見つけたら（`flags` が空でなければ）、その写真はデータセットから外します。誤検出に見えても戻しません（本人が確かめるのは、副業Aのストック販売の側です）。迷ったらフラグを立てるよう指示しています。見逃すより、余分に外すほうが安く済むからです。
4. **temperature 0、seed 固定。** 同じ画像・モデル・プロンプトなら同じキャプションが出るようにします。再生成や検証をやり直せるようにするためです。ただし、Ollama のバージョンや実行エンジン（MLX か llama.cpp か）が変わると、結果も変わることがあります。そこで生成ログに、モデルのタグとダイジェスト（`ollama list` の ID）、`ollama -v` の結果を残します。再試行のときだけ seed と temperature を変え、生成ログに残します。
5. **スキーマは最小限にしてある。** Ollama はJSONスキーマを文法に変えて出力を縛ります。ただ、使えるキーワードは一部だけで、使えないものは黙って無視されます（llama.cpp の説明では `uniqueItems` は非対応です）。そこで `type` / `properties` / `required` / `enum` / `items` / `additionalProperties` だけを使っています。文字数、文の数、flags の重複、文体は、生成スクリプトが `tools/validate_records.py` の `check_caption_output()` で毎回確かめます。
6. **形式の例を1つ入れた。** 7〜12Bのモデルは、ルールの文だけより実例があるほうが、文の数・長さ・常体を守りやすくなります。キーの形はスキーマで強制できますが、文体はできません。例の題材は屋内の料理です。季節や時間帯の手がかりがないので、雪・桜・夕焼けを呼び込みにくく、「見えなければ季節を書かない」の手本にもなります。「別の写真の例」「まねしない」とも明記しています。それでも語句をまねる心配はあるので、パイロットで確かめます（下の「注意すべき失敗」）。目立つようなら v2 で例を外すか、差し替えます。
7. **VLMには縮小コピーを渡す。** 渡すのは、向きを補正してメタデータを消したJPEGの縮小コピーです（長辺1280px前後。要確認：パイロットで1024pxや1536pxと比べてもよい）。HEICは渡せません。実行環境によってはEXIFの向きが無視されて、横倒しの画像として読まれます。画像を小さくすると画像のトークンが減って速くなり、`num_ctx` にも収まります。出荷用の画像（長辺2,048pxか原寸）とは別のものです。

## Ollama での呼び出し方

`/api/chat` に、システムプロンプト、ユーザープロンプト、base64にした画像、`format`（スキーマファイルの中身）、`options.temperature: 0`、`stream: false` を送ります。どちらの例も `dataset-b/` で実行します。

### curl（jq でリクエストを組み立てる）

```sh
# photo.jpg は向きを補正し、メタデータを消した縮小コピー
base64 < photo.jpg | tr -d '\n' > /tmp/img.b64
jq -n \
  --rawfile system prompts/caption_v1_system.txt \
  --rawfile user prompts/caption_v1_user.txt \
  --slurpfile schema schema/caption_output.v1.schema.json \
  --rawfile img /tmp/img.b64 \
  '{model: "qwen3-vl:8b-instruct", stream: false, format: $schema[0],
    options: {temperature: 0, seed: 42, num_ctx: 8192, num_predict: 1024},
    messages: [{role: "system", content: $system},
               {role: "user", content: $user, images: [$img]}]}' \
| curl -s http://localhost:11434/api/chat -H "Content-Type: application/json" -d @- \
| jq '{done_reason, caption: (.message.content | fromjson)}'
```

画像は `--arg` ではなくファイル経由で渡します。大きな画像だと、コマンドラインの長さの上限を超えるからです。macOS 15 以降は jq が最初から入っています（なければ `brew install jq`）。

### Python（標準ライブラリ + jsonschema）

```python
import base64, json, pathlib, sys, urllib.request
from jsonschema import Draft202012Validator

MODEL = "qwen3-vl:8b-instruct"  # パイロットで決めたタグ。この文字列をそのまま caption_model に記録する
THINK = None                    # thinking 対応モデル（ollama show の Capabilities に thinking がある）なら False

system = pathlib.Path("prompts/caption_v1_system.txt").read_text(encoding="utf-8")
user = pathlib.Path("prompts/caption_v1_user.txt").read_text(encoding="utf-8")
schema = json.loads(pathlib.Path("schema/caption_output.v1.schema.json").read_text(encoding="utf-8"))
validator = Draft202012Validator(schema)

def caption(image_path):
    img = base64.b64encode(pathlib.Path(image_path).read_bytes()).decode("ascii")
    body = {
        "model": MODEL,
        "stream": False,
        "format": schema,
        "options": {"temperature": 0, "seed": 42, "num_ctx": 8192, "num_predict": 1024},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user, "images": [img]},
        ],
    }
    if THINK is not None:
        body["think"] = THINK
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        res = json.load(resp)
    if res.get("done_reason") != "stop":
        raise RuntimeError(f"生成が途中で止まった（done_reason={res.get('done_reason')}）")
    out = json.loads(res["message"]["content"])
    validator.validate(out)  # 形のチェック。文字数・文の数・flags の重複は check_caption_output() で見る
    return out

if __name__ == "__main__":
    print(json.dumps(caption(sys.argv[1]), ensure_ascii=False, indent=2))
```

`num_predict` は暴走したときの歯止めで、`num_ctx` は画像のトークンが収まる余裕です（要確認：必要な量はモデルと画像の大きさで変わります）。Ollama がスキーマの `$schema` / `title` / `description` を受け付けずにエラーを返した場合は、その3つを消してから送ってください（要確認）。

### Apple Silicon での注意（2026年10月時点）

- Ollama v0.40.0（2026年9月25日）からは、Apple Silicon では MLX が対応している構造のモデル（gemma4、qwen3.5、qwen3.6 など）が、最初から MLX で動きます。qwen3-vl がどちらのエンジンで動くかは要確認です。
- MLX で動かすときの構造化出力には、不具合の報告があります。`format` が無視される（#16563、2026年8月に修正済みとして閉じられた）。MLX エンジンで構造化出力が止まらず、JSON の後に空白が `num_predict` まで続く（#18567。報告では英語のプロンプトでも12回中3回、英語以外で8回中5回、llama.cpp では0回。修正されずに「多くの推論エンジンの標準的な挙動」として閉じられた）。gemma4 は v0.40.0 から MLX が既定なので、パイロットで失敗率を必ず数え、安定しなければ llama.cpp で動くタグか MLX-VLM を使います。thinking を有効にすると、JSONの先頭に「.」が付く（#18441。`think: false` で出なくなる。v0.34.4 で対策が入った）。
- Homebrew 版の ollama では、MLX 用の xgrammar が入っておらず、構造化出力が使えないという報告があります。公式の配布版を使ってください。
- 対策として、どの応答でも `done_reason == "stop"`、JSONとして読めるか、スキーマに合うか、`check_caption_output()` でエラーが出ないかを確かめます。失敗したら seed を変えて（例 43、temperature 0.3）1回だけ再試行し、記録します。それでも駄目なら保留リストに入れます。パイロットでは失敗率も数えます。Ollama で安定しない場合は、MLX-VLM（`pip install -U mlx-vlm`、`mlx_vlm.server` は OpenAI 互換の `/v1/chat/completions` で `json_schema` を受け付けます）に乗り換えることもできます（要確認：使いたいモデルに対応しているか）。

## パイロットの手順（モデル選び）

候補は次の2つです。どちらもメモリ24GBのMacで動く大きさです。1つずつ動かします（`ollama stop <モデル>` で外す）。

| 候補 | Ollama のタグ | ダウンロード | 動かすときのメモリの目安 | メモ |
|---|---|---|---|---|
| A | `qwen3-vl:8b-instruct` | 約6.1GB | 約8〜12GB（要確認） | Qwen3-VL 8B の instruct 版（考える過程を出さない）。Ollama 0.12.7 以上が必要。国産の LLM-jp-4-VL 9B の beta 版の評価で、日本語の平均が同程度とされた比較相手（ABCI FY25 の報告。正式版のモデルカードの比較相手は別のモデル） |
| B | `gemma4:12b` | 約7.6GB（二次情報なので要確認） | 約10〜14GB（要確認） | Google Gemma 4 の 12B（2026年6月公開）。画像入力に対応し、140以上の言語を扱う。Apple Silicon では MLX で動く（上の注意を参照） |

予備の候補：`qwen3.5:9b`（約6.6〜7.6GB、画像入力に対応、thinking が最初から有効なので `think: false` を指定）。LLM-jp-4-VL 9B（国産、thinking 型。公開ページには beta の表記がある。ライセンスは二次情報では Apache-2.0。Ollama や MLX で動くかは要確認）。`qwen3.8:27b`（約18GB。メモリ32GB以上で品質が足りないときの候補。速度は要確認）。

1. **写真を50枚選ぶ。** 副業Aで「可」になった写真から無作為に選びます。分類（風景・街並み・建物・鉄道・乗り物・食・植物・屋内）がそれぞれ数枚ずつ入るようにします。季節の手がかりがない写真や夜の写真も入れます。
2. **2つのモデルで同じ50枚を生成する。** プロンプト（caption-v1）と設定（temperature 0、seed 42）は同じにします。1枚あたりの秒数と、使ったメモリ（アクティビティモニタ）も記録します。
3. **機械的に検査する。** JSONとして読めない、`done_reason` が `stop` でない、スキーマに合わない、文字数・文の数の違反、の件数を数えます。
4. **ざっと判定する（判定するのは本人で、AIではない）。** どちらのモデルの出力か分からないように、A/B の名前を伏せて並べ替えます。判定の基準は、データシートと同じ3段階（正しい／軽微な誤り／重大な誤り）です。1行の判定は、4項目（ja_short・ja_detailed・en_short・category）のうち最も悪いものにします。
5. **選ぶ。** 重大な誤りの少ないほうを選びます。50枚では差の見分けが荒いので、差が2〜3件以内なら、軽微な誤り、日本語の自然さ、速度で決めます。どちらも重大な誤りが多すぎる場合は（目安は1割超）、プロンプトを v2 にするか、予備の候補を試します。
6. **記録する。** 決めたタグを、そのまま `caption_model` に入れます（例：`"qwen3-vl:8b-instruct"`）。ダイジェストと Ollama のバージョンは生成ログに残します。1枚あたりの秒数から、全件（最大約6,500枚）にかかる時間を見積もります。

パイロットの判定はモデル選びにだけ使います。データシートに書く誤り率は、出荷するキャプションから無作為に抜いた300件で別に測ります。

## バージョン管理のルール

- プロンプト（system と user）を1文字でも変えたら、新しいファイル `caption_v2_system.txt` と `caption_v2_user.txt` を作り、`prompt_version` を `"caption-v2"` にします。出力の形を変えるときは `schema/caption_output.v2.schema.json` も作ります。
- v1 で作ったキャプションを出荷したり検証したりした後は、v1 のファイルを書き換えてはいけません（誤字の直しも含みます）。各行の `prompt_version` から、そのキャプションを作ったプロンプトを必ずたどれるようにするためです。
- モデルだけを変えたときは `prompt_version` はそのままで、`caption_model` が変わります。
- 分類ごとに作り直した結果、v1 と v2 の行が混ざってもかまいません。ただし、300件の検証は出荷する最終版のキャプションから抜き直して、判定し直します。

## 注意すべき失敗

- **固有名詞**：看板の文字の書き写し、有名な場所や寺社の名前、路線名・車両の形式名、「Japan」「Japanese」。
- **見えない季節や時間帯を言い切る**：写っていない雪・桜・紅葉・夕焼け・夜景。曇り空を「夕暮れ」とする、緑の木を「新緑」とする、など。
- **人について書く**：「観光客」「人々」「歩く人」。flags を立てずに文章に書いてしまう。
- **英語が内容を足す**：en_short に、ja_short にない形容詞・場所・季節（例：beautiful、in Kyoto、in autumn）が入る。
- **形式の例をまねる**：「木のテーブル」「白い器」「落ち着いた雰囲気」が、関係ない写真で不自然に何度も出る。
- **書き方の違反**：「この写真は」で始まる、「写っている」「撮影」を使う、「です・ます」、文の数や文字数の違反、6個以上のものを数字で数える。
- **分類の取り違え**：landscape と flora（紅葉の山）、cityscape と architecture、railway と landscape（列車の入った風景）。
- **出力の不具合**：`done_reason` が `length`（空白が続いて止まらない）、JSONの前に余計な文字が付く、`flags` に同じ値が2回入る。

## 参考

- Ollama 構造化出力：https://github.com/ollama/ollama/blob/main/docs/capabilities/structured-outputs.mdx
- Ollama API（`/api/chat`、`images`、`format`、`think`）：https://github.com/ollama/ollama/blob/main/docs/api.md
- llama.cpp のJSONスキーマ対応（非対応のキーワードは黙って無視される）：https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md
- MLX 構造化出力の不具合：https://github.com/ollama/ollama/issues/16563 、https://github.com/ollama/ollama/issues/18567 、https://github.com/ollama/ollama/issues/18441 、https://github.com/Homebrew/homebrew-core/issues/302645
- Ollama v0.40.0（Apple Silicon で MLX が既定）：https://github.com/ollama/ollama/releases/tag/v0.40.0
- LLM-jp-4-VL 9B：https://abci.ai/ja/acceleration_program/fy25/FY25-52.pdf 、https://huggingface.co/llm-jp/llm-jp-4-vl-9b
- MLX-VLM：https://github.com/Blaizzy/mlx-vlm
