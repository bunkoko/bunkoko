# M6 の作り（1本を手で通す）

SPEC §8.8 G7・§8.9 音声・§8.10 映像・§8.11 公開用キット・§8.12 人の確認の作り方。
部品のあいだの受け渡しの形は src/koten/media/model.py（このファイルと一緒に読む）。

## 利用者の判断（2026-11-20）

- 語りの声：利用者が VOICEVOX で落ち着いた声を試聴して選ぶ。決まるまでは settings の `[voicevox] speaker_id`。
  クレジットはキャラクターごとの規約に従い、settings の `[voicevox] credit` に人が書く（決まるまで空。空なら確認画面で止める）。
- 旧字・旧仮名の一節：画面には原文。読み上げは、言葉は変えずに仮名づかいと字体だけをプログラムで今に直す
  （AI に書かせない）。読み違いは確認画面で人が聞いて、config/readings.csv で直す。
- 説明欄：D-036 の案どおり（SPEC §8.11 の①〜⑪）。AI に新しい文は書かせない。
- BGM：最初の1本には入れない（あとから足せる作り）。

## 流れ

```
台本（script_done）
  → koten voice   ：読み上げの準備 → VOICEVOX → voice/*.wav・voice/manifest.json（state voiced）
  → koten visuals ：カード・写真 → cards/・photos/・visuals/shots.json
  → koten render  ：ffmpeg → render/*.mp4、G7（尺）→ verification_json（state rendered）
  → koten kit     ：説明欄・チャプター・字幕・サムネイル → kit/
  → koten review  ：確認画面 → exports/<回>/review.html
  → koten episode approve / reject（state reviewed / 差し戻し）
```

`koten produce --episode E…` で voice から review まで続けて動かす。

## 共通の決まり

- HDD のパス（hdd_root・photos_dir）は settings から。検索と API（koten serve）はこれらを読まない。
- 外部サイトへのアクセスはしない。カードとサムネイルの HTML は、外のフォント・画像・スクリプトを読まない
  （文字は Mac のヒラギノなど手元のフォントを font-family で並べる。Google Fonts は使わない）。
  画像は file:// か data: で渡す。VOICEVOX は 127.0.0.1 のローカルのエンジン。
- 本文（一節・出典・台本の文）を HTML に入れるときは必ずエスケープする（html.escape）。
- VOICEVOX の audio_query と synthesis には同じ speaker ID を渡す（CLAUDE.md）。
- 中間ファイル（音声・カード・章の動画）も HDD に残す（D-031）。
- テストは小さく速く。VOICEVOX は httpx.MockTransport で偽物にする。ffmpeg・Playwright の要るテストは、
  使えなければ skip する（`shutil.which("ffmpeg")`、Playwright の起動に失敗したら skip）。

## 部品

### koten.voice（音声）

`src/koten/voice/reading.py`：読みの直し（純粋な関数。外を読まない）

- `modernize(text: str) -> str`：旧仮名づかいを今の仮名づかいに、旧字体を新字体に、くの字点（〳〵・〴〵）と
  踊り字（ゝゞヽヾ）を開く。言葉は変えない。直せないものはそのまま残す（推測で書き換えない）。
  - 旧字体 → 新字体：常用漢字表の旧字体の対応表（氣→気・國→国・學→学・會→会・來→来・爲→為・體→体・聲→声・
    惡→悪・樂→楽・實→実・當→当・與→与・歸→帰・廣→広・對→対・讀→読・傳→伝・發→発・晝→昼 など。150字以上）。
  - 仮名：ゐ→い・ゑ→え（を は「を」のまま）。語中・語尾のハ行（思ふ→思う・言ひ→言い・添はず→添わず・
    考へ→考え・顔→かほ→かお は かな書きのときだけ）。長音（さう→そう・かう→こう・やう→よう・らう→ろう・
    たう→とう・なう→のう・まう→もう・はう→ほう・きう→きゅう・しう→しゅう・ちう→ちゅう・りう→りゅう・
    けふ→きょう・てふ→ちょう・せう→しょう・でせう→でしょう・ませう→ましょう・ぢ→じ・づ→ず（語頭や連濁は
    確かな規則だけ））。「ちやん」→「ちゃん」のような大きく書いた拗音・促音は、確かな語の並び（ちやん・
    しやう・きやう・ませう・つた→った のうち語の切れ目が確かなもの）だけ直す。迷うものは直さない。
  - 助詞の「は」「へ」は今の書き方でも「は」「へ」なので、そのまま（VOICEVOX が「わ」「え」と読む）。
- `speech_text(text: str) -> str`：読み上げ用の整え：【】や記号の除去、「 / 」→「、」、別名の括弧
  （「岡倉天心（岡倉覚三）」の括弧の中）を落とす、連続する空白を詰める。かぎかっこはそのまま。
- `load_overrides(path: Path) -> dict[str, str]`：config/readings.csv（列：key・reading・sha・note）。key は
  「回の ID:段落の ID」（その回だけ）か候補の ID（どの回でも）。sha（画面の文の sha1 の頭8字）がある行は、
  画面の文がその sha のときだけ使う（`override_for`）。無ければ空の dict。
- テスト：tests/test_voice_reading.py。上の例すべて、迷うものを直さないこと、言葉を変えないこと。

`src/koten/voice/voicevox.py`：エンジンとの話（httpx）

- `class VoicevoxClient(endpoint: str, speaker_id: int, *, client: httpx.Client | None = None, timeout: float = 60)`
  - `version() -> str`（GET /version）
  - `audio_query(text: str) -> dict`（POST /audio_query?text=…&speaker=…）
  - `synthesis(query: dict) -> bytes`（POST /synthesis?speaker=…。query は JSON 本文。audio_query と同じ speaker）
  - `register_words(words: list[tuple[str, str]]) -> int`：ユーザー辞書（GET /user_dict で既にある表層形を見て、
    無いものだけ POST /user_dict_word?surface=…&pronunciation=…（カタカナ）&accent_type=0&word_type=PROPER_NOUN）。
    足した数を返す。カタカナでない読みはひらがな→カタカナにしてから送る。読みが空なら送らない。
- `class VoiceError(RuntimeError)`：つながらない・失敗したときに、何をすればよいかの文で出す。

`src/koten/voice/synth.py`：1回分の合成

- `synthesize(lines: list[Line], out_dir: Path, client: VoicevoxClient, *, episode_id: str, speed_scale: float = 1.0,
  sample_rate: int = 24000, pause_sentence: float = 0.25, pause_segment: float = 0.6, pause_chapter: float = 1.2,
  log=print) -> VoiceManifest`
  - out_dir は media_dir。voice/<chapter_id>.wav と voice/manifest.json を書く。
  - 段落ごとに `pair_sentences(display, reading)` で文に分け、文ごとに audio_query → speedScale・
    outputSamplingRate・outputStereo=false を入れる → synthesis。wav（16bit PCM・モノラル）を読み、無音を挟んで
    章の wav につなぐ。文の時刻・段落の時刻を、実際の長さ（フレーム数 / サンプリング周波数）から記録する。
  - Line.audio_path がある段落（人の一言を自分の声で）は合成せず、その wav を（サンプリング周波数をそろえて）入れる。
    周波数が違えば VoiceError（ffmpeg で変換してから、と案内）。
  - 章の終わりに pause_chapter の無音。offset は前の章の duration の合計。
  - 同じ段落の合成をやり直さずに済むよう、文ごとの wav を voice/cache/<sha1(speaker・speed・reading)>.wav に残し、
    あれば使う。
- テスト：tests/test_voice_synth.py（偽のエンジンが、文字数に比例した長さの無音の wav を返す）。時刻の足し算、
  同じ speaker が両方に渡ること、キャッシュ、人の声の wav、エラーの文。

### koten.visuals（画面）

`templates/`（リポジトリの直下）：`quote_card.html`（縦書き。writing-mode: vertical-rl。一節・出典・訳の表示・
ライセンスの表示）、`title_card.html`（章タイトル）、`chart_card.html`（統計：大きな数字と単位・ラベル・
「出典：調査名（年）」。数字は stat の値をそのまま）、`thumb_a.html`・`thumb_b.html`・`thumb_c.html`
（1280×720。悩みの言葉・著者名・背景の写真）。差し込みは `string.Template`（$name）で、値はすべてエスケープ。
外のファイルを読まない（写真は file:// の絶対パスか data: URI）。

`src/koten/visuals/cards.py`

- `render_cards(specs: list[CardSpec], media: Path, *, width=1920, height=1080, templates_dir: Path) -> dict[str, str]`：
  CardSpec ごとに cards/<seg_id>.png を書き、seg_id → 相対パスを返す。Playwright（sync API・chromium）を1回だけ
  起こして全部を撮る。長い一節は文字の大きさを段階的に下げて収める（はみ出したら小さくする。最後は切らずに
  警告を返す）。
- `render_thumbnails(specs: list[ThumbSpec], media: Path, *, templates_dir: Path) -> list[str]`：kit/thumb_<n>.png
  （1280×720）。
- `class CardError(RuntimeError)`：Playwright が無い・ブラウザが無いとき、`uv sync --extra ml --extra video` と
  `uv run playwright install chromium` を案内する。

`src/koten/visuals/photos.py`

- `list_photos(photos_dir: Path) -> list[Path]`（jpg・jpeg・png・webp・heic。隠しファイルを除く。並びは安定）
- `pick_photos(photos: list[Path], exclude: set[str], n: int, seed: str, photos_dir: Path) -> list[Path]`：
  exclude（直近の回で使った写真の photos_dir からの相対パス）を除いて、seed（回の ID）で決まる順に n 枚。
  足りなければ、この回の中で使い回す（直近の回の写真は使わない。それでも0枚なら空）。
- `prepare_photo(src: Path, dst: Path, width: int, height: int) -> None`：1920×1080 に切り抜いた jpg を書く
  （ffmpeg の scale と crop。HEIC は Mac の sips で jpg にしてから。どちらも無ければ PhotoError）。

`src/koten/visuals/shots.py`

- `build_shots(manifest: VoiceManifest, cards: dict[str, str], photos: list[str], *, width, height, fps,
  min_shot=5.0, max_shot=10.0) -> ShotList`
  - 章ごとに [0, duration] を、すき間なく重ならずに覆う。段落の区切りは段落の start（最初の段落は 0）。
    章の終わりの間は最後のショットに含める。
  - カードのある段落（cards に seg_id がある）は、その段落の区間をそのカード1枚（still）。
  - 写真の段落（visual.type が photo、author_card・page_image も今は写真）は、続く写真の段落をまとめた区間を
    min_shot〜max_shot 秒（目安7秒）ごとに区切り、写真を順に当てる（zoom）。短すぎる端は前のショットに足す。
  - photos は media からの相対パス（prepare 済み）。写真が0枚なら title_card の色だけの画像を使えるよう、
    統合の側が予備の画像を1枚渡す（photos が空なら ValueError）。
- テスト：区間の覆い方（合計が章の長さに一致・重ならない）、写真の切り替えの間隔、カードの区間。

### koten.render（組み立て）

`src/koten/render/ffmpeg.py`

- `render_episode(media: Path, manifest: VoiceManifest, shots: ShotList, *, bitrate="8M", ffmpeg="ffmpeg",
  log=print) -> Path`
  - ショットごとに、画像から長さちょうどの動画を作る（1920×1080・fps・yuv420p・libx264）。フレームは、章の頭からの
    時刻を round(t*fps) で区切り、ずれがたまらないようにする。写真（zoom）は zoompan でゆっくり寄る
    （1.00→1.06 程度）。カード（still）は動かさない。
  - 章ごとにショットをつなぎ（concat demuxer）、章の wav をのせて render/<chapter_id>.mp4（AAC 192k）。
  - 章をつないで render/<episode_id>.mp4（-c copy）。できた長さを ffprobe で確かめる。
  - 途中のファイルは render/tmp/ に置き、終わったら消す。
- `probe_duration(path: Path, ffprobe="ffprobe") -> float`
- `g7_issues(manifest: VoiceManifest, chapter_ranges: dict[str, tuple[float, float]], total=(600.0, 900.0)) -> list[str]`：
  合計が範囲の外、章の長さが範囲の外（範囲は統合の側が formats.yaml から作る）を、日本語の文で返す。
- `class RenderError(RuntimeError)`
- テスト：小さな画像と短い wav で1章2ショットの動画を作り、長さを確かめる（ffmpeg が無ければ skip）。g7_issues。

### koten.publish_kit（公開用キット）

`src/koten/publish_kit/kit.py`

- `chapter_lines(manifest: VoiceManifest) -> list[str]`：「00:00 タイトル」。00:00 から、昇順、10秒未満の章は
  前の章にまとめる、3つ未満なら空（YouTube のチャプターの条件。D-007）。1時間を超えたら h:mm:ss。
- `build_description(kit: KitInput, manifest: VoiceManifest) -> str`：SPEC §8.11 の①〜⑪の順。
  ①つかみ ②約束 ③▼目次（chapter_lines） ④▼この動画で読んだ古典（作品ごとに1行：著者『作品』（訳者訳）
  「章」／底本／URL／ライセンス、訳の表示） ⑤▼この動画で使った調査・データ ⑥▼背景と昔の資料の出典
  ⑦▼参考にした本（books があるときだけ。見出しのすぐ上に「（広告）」と amazon_notice）⑧クレジット
  ⑨注意書き ⑩問いかけ ⑪ハッシュタグ（最大3つ。# を付ける）。空の節は見出しごと出さない。
  5000字を超えたら DescriptionError（YouTube の上限は D-036 で確かめる）。
- `build_srt(manifest: VoiceManifest, *, max_chars=28) -> str`：文ごとの字幕。長い文は max_chars で区切って
  2行まで、それ以上は時間を文字数で割って次の字幕に。時刻は章の offset を足す。
- `ad_break_seconds(manifest) -> list[float]`：章の切れ目（最初を除く）。
- `write_kit(kit: KitInput, manifest: VoiceManifest, media: Path) -> dict[str, str]`：kit/description.txt・
  kit/subtitles.srt・kit/chapters.txt を書き、パスを返す。
- テスト：チャプターの条件、説明欄の並びと空の節、字幕の時刻と分け方、上限。

### koten.review（確認画面）

`src/koten/review/page.py`

- `build_review_html(bundle: ReviewBundle, out_path: Path) -> Path`：1枚の HTML（外を読まない。動画と画像は
  out_path からの相対パス）。中身：タイトル・状態、承認できない理由（blockers、目立つように）、注意、
  動画（<video controls>）、サムネイル、台本の全文、一節の原文と読み上げの対応（旧仮名の直しを人が聞いて確かめる）、
  出典とライセンス、ゲートの結果（G1〜G7）、説明欄・チャプター、広告位置の候補、人の一言の状態、
  承認・差し戻しのコマンド（コピーして打つ。このページから DB は変えない）。すべてエスケープ。
- `src/koten/review/actions.py`：
  - `approve(conn, episode_id: str, *, note: str = "", blockers: list[str]) -> None`：state が rendered のときだけ。
    blockers があれば ReviewError（理由を並べる）。state を reviewed、reviewer_note に note。
  - `reject(conn, episode_id: str, *, note: str, to_state: str = "script_done") -> None`：to_state は planned か
    script_done。note は必須。
  - `set_human_take(conn, episode_id: str, *, text: str | None = None, audio_path: str | None = None,
    skip: bool = False) -> dict`：episodes.human_take（JSON）と script_json の human_take を更新。text と audio_path
    のどちらか、または skip。
  - `class ReviewError(RuntimeError)`
- テスト：HTML のエスケープ・相対パス・blockers の表示、approve・reject・set_human_take の状態の移り方。
