"""読みの直し（koten.voice.reading）：旧字・旧仮名・踊り字、読み上げの整え、人が直した読み。

直すもの（言葉は変えずに字体と仮名づかいだけ）と、迷うので直さないもの（語の切れ目が分からない
もの・今の仮名づかいの文）の両方を確かめる。
"""

from __future__ import annotations

import pytest

from koten.media.model import split_sentences
from koten.voice.reading import (
    KYUJI,
    load_overrides,
    modernize,
    override_for,
    speech_text,
    text_sha,
)

# --- 旧字体 → 新字体 ------------------------------------------------------------


def test_kyuji_table_is_large_and_one_way():
    assert len(KYUJI) >= 150
    assert all(len(k) == 1 and len(v) == 1 and k != v for k, v in KYUJI.items())
    assert not set(KYUJI) & set(KYUJI.values())  # 直した字をもう一度直すことがない


@pytest.mark.parametrize(
    "old,new",
    [
        ("氣", "気"),
        ("國", "国"),
        ("學", "学"),
        ("會", "会"),
        ("來", "来"),
        ("爲", "為"),
        ("體", "体"),
        ("聲", "声"),
        ("惡", "悪"),
        ("樂", "楽"),
        ("實", "実"),
        ("當", "当"),
        ("與", "与"),
        ("歸", "帰"),
        ("廣", "広"),
        ("對", "対"),
        ("讀", "読"),
        ("傳", "伝"),
        ("發", "発"),
        ("晝", "昼"),
        ("辯", "弁"),
        ("辨", "弁"),
        ("顏", "顔"),
        ("歲", "歳"),
    ],
)
def test_kyuji_examples(old, new):
    assert modernize(old) == new


def test_kyuji_in_text():
    assert modernize("國の爲に盡す") == "国の為に尽す"


def test_compat_ideographs_and_combining_dakuten():
    # 互換漢字（神 U+FA19・社 U+FA4C）と、分けて書いた濁点（か＋U+3099）
    assert modernize("\ufa19\ufa4c") == "神社"
    assert modernize("か\u3099") == "が"


def test_kept_joyo_forms():
    assert modernize("塡剝頰") == "塡剝頰"  # 常用漢字表でこの字体が正しい


# --- 踊り字 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "src,want",
    [
        ("こゝろ", "こころ"),
        ("たゞ", "ただ"),
        ("などゝ", "などと"),  # 濁音のあとの「ゝ」は清音
        ("いすゞ", "いすず"),
        ("ハヽヽ", "ハハハ"),
        ("イタヾク", "イタダク"),
        ("ます〳〵", "ますます"),
        ("しみ〴〵", "しみじみ"),
        ("とき〴〵", "ときどき"),
        ("いよ〱", "いよいよ"),  # 1字のくの字点
        ("しみ〲", "しみじみ"),
        ("ます／＼", "ますます"),  # 横組みの代用表記
        ("しみ／″＼", "しみじみ"),
        ("ところ〴〵", "ところどころ"),  # 3字の語
        ("ひとつ〳〵", "ひとつひとつ"),
        ("一つ〳〵", "一つ一つ"),
        ("むしゃ〳〵", "むしゃむしゃ"),  # 小さい字は前の字と1拍
        ("ちり〴〵", "ちりぢり"),  # 連濁の ぢ・づ はそのまま
        ("つく〴〵", "つくづく"),
        ("ドン〳〵", "ドンドン"),
        ("かへす〴〵", "かえすがえす"),
        ("人〻", "人々"),
        # 旧仮名の本は拗音を大きく書く（むしや＝むしゃ）。3字で繰り返す
        ("むしや〳〵", "むしやむしや"),
        ("ごちや〳〵", "ごちやごちや"),
        ("めちや〳〵", "めちやめちや"),
        ("ぐちや〳〵", "ぐちやぐちや"),
        ("くちや〳〵", "くちやくちや"),
        ("がちや〳〵", "がちやがちや"),
        ("ぎゆう〳〵", "ぎゆうぎゆう"),
        ("ひゆう〳〵", "ひゆうひゆう"),
        ("じやら〳〵", "じやらじやら"),
        ("ひよこ〳〵", "ひよこひよこ"),
        ("かさね〴〵", "かさねがさね"),
        ("かへる〳〵", "かえるかえる"),
        ("かへる〴〵", "かえるがえる"),
        ("にや〳〵", "にやにや"),  # や で終わる2字はそのまま開く
        ("いや〳〵", "いやいや"),
        # 開いたあとの決まった語
        ("なほ〳〵", "なおなお"),
        ("なほなほ", "なおなお"),
        ("かはる〴〵", "かわるがわる"),
        ("かはるがはる", "かわるがわる"),
    ],
)
def test_odoriji(src, want):
    assert modernize(src) == want


@pytest.mark.parametrize(
    "text",
    [
        "〳〵",  # 前に字が無い
        "ゝ",
        "時〳〵",  # 漢字1字か2字か分からない
        "途切れ〳〵",  # 2字か3字か分からない
        "かちり〳〵",  # 「ちり」か「かちり」か分からない
        "ポトン〳〵",  # カタカナの擬音は3字のことが多い
        "んゝ",
        # い段の字のあとの大きい や・ゆ・よ で始まる2字は、拗音（にゅう・きょと）の途中かもしれない
        "にゆう〳〵",
        "きよと〳〵",
        "ぴよこ〳〵",
        "しゆう〴〵",
    ],
)
def test_odoriji_left_when_unclear(text):
    assert modernize(text) == text


# --- 旧仮名 → 今の仮名 ------------------------------------------------------------

CONVERT = [
    # ゐ・ゑ（を はそのまま）
    ("ゐる", "いる"),
    ("こゑ", "こえ"),
    ("植ゑる", "植える"),
    ("ゆゑに", "ゆえに"),
    ("ヷイオリン", "ヴァイオリン"),
    # ハ行（漢字の動詞の送り仮名）
    ("思ふ", "思う"),
    ("言ひ出す", "言い出す"),
    ("添はず", "添わず"),
    ("考へる", "考える"),
    ("与へる", "与える"),
    ("行け、戦へ、", "行け、戦え、"),  # 命令（戦へ出る は名詞＋助詞なので直さない）
    ("思はう。", "思おう。"),
    ("言はれた", "言われた"),
    ("構はない", "構わない"),
    ("片思ひ", "片思い"),
    ("向かふ", "向かう"),
    ("味はふ", "味わう"),
    ("住まひ", "住まい"),
    ("恥ぢらふ", "恥じらう"),
    # ハ行（かなで書く語）
    ("といふ", "という"),
    ("かういふ事", "こういう事"),
    ("彼がかう言つた", "彼がこう言った"),  # を・が のあとは後ろの語で確かなものだけ
    ("野郎がかう大勢いる", "野郎がこう大勢いる"),
    ("事がかうなつては", "事がこうなっては"),
    ("それをかうして", "それをこうして"),
    ("それをかう考へる", "それをこう考える"),
    ("などいふ", "などいう"),
    ("とはいへ", "とはいえ"),
    ("といはれ", "といわれ"),
    ("いはゆる", "いわゆる"),
    ("おもひ出", "おもい出"),
    ("かほを洗ふ", "かおを洗う"),
    ("その人のかほが", "その人のかおが"),
    ("そのうへ", "そのうえ"),
    ("おまへ", "おまえ"),
    ("なほ、", "なお、"),
    ("すなはち", "すなわち"),
    ("あはれ", "あわれ"),
    ("しあはせ", "しあわせ"),
    ("かまはない", "かまわない"),
    ("まちがひ", "まちがい"),
    ("てしまふ", "てしまう"),
    ("見たまへ", "見たまえ"),
    ("とほく", "とおく"),
    ("おほきな", "おおきな"),
    ("かへる", "かえる"),
    ("家にかへる", "家にかえる"),
    ("どこかへしまつた", "どこかへしまった"),  # か＋助詞の へ はそのまま
    ("とさへ", "とさえ"),
    ("にほひ", "におい"),
    # 長音
    ("さうだ", "そうだ"),
    ("嬉しさうに", "嬉しそうに"),
    ("かうして", "こうして"),
    ("私はかう思ふ", "私はこう思う"),
    ("かのやうに", "かのように"),
    ("同じやうな", "同じような"),
    ("思ふやうに", "思うように"),
    ("あるだらう。", "あるだろう。"),
    ("さうだらうか", "そうだろうか"),
    ("であらう。", "であろう。"),
    ("よからう。", "よかろう。"),
    ("ゐたらう。", "いたろう。"),
    ("行かう。", "行こう。"),
    ("死なう。", "死のう。"),
    ("読まう。", "読もう。"),
    ("帰らうか", "帰ろうか"),
    ("見やうと", "見ようと"),
    ("どうしやう。", "どうしよう。"),
    ("ほんたう", "ほんとう"),
    ("ありがたう", "ありがとう"),
    ("たうとう", "とうとう"),
    ("やうやく", "ようやく"),
    ("からうじて", "かろうじて"),
    ("けふは", "きょうは"),
    ("てふてふ", "ちょうちょう"),
    ("でせう", "でしょう"),
    ("ませう", "ましょう"),
    ("うれしうございます", "うれしゅうございます"),
    # 促音（大きく書いた「つ」）
    ("言つた", "言った"),
    ("持つて", "持って"),
    ("あつた。", "あった。"),
    ("なつて", "なって"),
    ("だつた", "だった"),
    ("よかつた", "よかった"),
    ("わかつて", "わかって"),
    ("にとつて", "にとって"),
    ("やがつて", "やがって"),
    ("たつた一人", "たった一人"),
    ("ちよつと", "ちょっと"),
    ("やつぱり", "やっぱり"),
    ("はつきり", "はっきり"),
    ("まつたく", "まったく"),
    ("彼はきつと来る", "彼はきっと来る"),
    ("子供つぽい", "子供っぽい"),
    ("思つたことが", "思ったことが"),  # た＋こと
    ("言つたこと", "言ったこと"),
    ("あつたことを", "あったことを"),
    ("曾つて", "曽つて"),  # 嘗て（かつて）。旧字の 曾 は 曽 に直すが、つ は大きいまま
    ("未だ曾つて無い", "未だ曽つて無い"),
    # 拗音（大きく書いた「や」「よ」）
    ("ちやん", "ちゃん"),
    ("ぢやない", "じゃない"),
    ("さうじやない", "そうじゃない"),
    ("それは困つたことぢや", "それは困ったことじゃ"),  # こと＋ぢや（とぢ＝閉ぢ ではない）
    ("そとぢやない", "そとじゃない"),
    ("もとぢや。", "もとじゃ。"),
    ("目をとぢて", "目をとじて"),  # 閉ぢて
    ("とぢこもる", "とじこもる"),
    ("おぢや", "おじや"),
    ("ちやうど", "ちょうど"),
    # ぢ・づ
    ("恥ぢる", "恥じる"),
    ("しづか", "しずか"),
    ("少しづつ", "少しずつ"),
    ("まづ", "まず"),
    ("先づ", "先ず"),
    ("わづか", "わずか"),
    ("いづれ", "いずれ"),
    ("みづから", "みずから"),
    ("はづ", "はず"),
    ("あぢ", "あじ"),
]


@pytest.mark.parametrize("src,want", CONVERT)
def test_kana(src, want):
    assert modernize(src) == want


UNCHANGED = [
    # 助詞の「は」「へ」と「を」
    "人は山へ行く。",
    "私は家へ帰る。",
    "本を読む。",
    "をとこ",
    # 「つ」が本当に つ の語
    "あつい",
    "あつく",
    "あつさ",
    "つたない",
    "一つたりとも",
    "彼はかつて",  # 嘗て
    "いまだかつて",
    "曽つて",  # 曽て・嘗て（かつて）
    "嘗つて",
    "未だ嘗つて無い",
    "筆つたなく",  # た＋な は「つたない」なので直さない
    # 語の切れ目しだいで読みが変わる並び
    "ずつと",  # ずっと か ずつ＋と
    "もつと",  # もっと か 持つと
    "やつと",  # やっと か 奴と
    "さつき",  # 皐月
    "しやうがない",  # 仕様（しよう）か しょう か
    "何かうまい",
    "私はうれしい",
    "きれいなうた",
    "そのままうけとる",
    "したらうれしい",
    "大将がうとむ",
    "それぢやうまく",
    "どこかへ行く",
    "何処かへそれて",
    "誰かへ渡す",
    "なかまへ入る",  # 仲間＋助詞の へ
    "戦へ出る",  # 戦（いくさ）＋助詞の へ
    "戦へ赴く",
    "子供らへ",
    # 今の仮名で書いた「買う・飼う」（旧字新仮名の本）
    "本をかう時",
    "失笑をかうくらい",
    "母がかうでしょう",
    "何かほしい",
    "いかほど",
    "なかほど",
    "うちうみ",
    "しうち",
    "ぽつぽつ",
    "一つぽつんと",
    "ひどいはずだ",
    "ひどいひと",
    "ひどいふうに",
    # 熟語＋助詞の「へ」「は」、名詞＋助詞
    "宗教へ",
    "時代へ",
    "子の代へ",
    "その会へ",
    "向へ行く",
    "この歌はなかなか",
    "この会は",
    "相違はない",
    "ちがいはない",
    "おもいはない",
    # 今の仮名でも ぢ・づ のもの
    "つづく",
    "ちぢむ",
    "はなぢ",
    "みかづき",
    "こづつみ",
    "いづつ",  # 井筒
    "はなぢや",  # 鼻血や
    "はづき",
    "気づく",
    # カタカナの ヰ・ヱ（外来語では ヴィ・ウィ）
    "ダ・ヰンチ",
    "ヰスキー",
]


@pytest.mark.parametrize("text", UNCHANGED)
def test_left_as_is(text):
    assert modernize(text) == text


def test_modern_text_is_unchanged():
    text = (
        "向かうと、もらうか、しまうから、違うが、笑うね。向こうへ行こう。"
        "行なう。損なう。歌う。使う。味わう。語らう。住まう。"
        "そういうことだと思う。きっと、ずっと、もっと考えたい。"
        "言ってしまった。かつて訪れた町は、ちょうど暑かった。少しずつ近づく。"
    )
    assert modernize(text) == text


def test_passage():
    src = (
        "関さんは僕の所へやつてきて、もう、とても我慢がならないからほかの口を探してくるといふのであつた。"
        "さうして、前後二度、ほんとに勤め口を見つけだして姿を消した。"
    )
    want = (
        "関さんは僕の所へやってきて、もう、とても我慢がならないからほかの口を探してくるというのであった。"
        "そうして、前後二度、ほんとに勤め口を見つけだして姿を消した。"
    )
    assert modernize(src) == want


def test_old_kanji_and_kana_together():
    src = "兎に角こゝいらでまごついてゐるのはよせよ。早く東京へ歸つたらどうか。"
    assert modernize(src) == "兎に角ここいらでまごついているのはよせよ。早く東京へ帰ったらどうか。"


def test_idempotent_and_keeps_sentences():
    src = "いよ〳〵三月になつた。「さうでせう」と彼は云つた。思ふやうに行かぬ。"
    once = modernize(src)
    assert modernize(once) == once
    assert len(split_sentences(once)) == len(split_sentences(src))  # 文の数は変わらない


# --- 読み上げの整え ---------------------------------------------------------------


@pytest.mark.parametrize(
    "src,want",
    [
        # 別名の括弧は落とし、訳者の括弧は残す
        (
            "岡倉天心（岡倉覚三）『茶の本』（村岡博訳）「第一章」",
            "岡倉天心『茶の本』（村岡博訳）「第一章」",
        ),
        ("徳冨蘆花（徳冨健次郎）・岡倉天心『本』", "徳冨蘆花・岡倉天心『本』"),
        ("著者：岡倉天心（岡倉覚三）", "著者：岡倉天心"),
        # 見出しの区切り
        ("第十一講 / 東海道中膝栗毛のこと", "第十一講、東海道中膝栗毛のこと"),
        ("大見出し／中見出し", "大見出し、中見出し"),
        # 記号・ダッシュ
        ("【第一章】孤独について", "第一章孤独について"),
        ("※注意★", "注意"),
        ("字が〓だった", "字がだった"),
        ("思った――そうだ。", "思った、そうだ。"),
        ("と言った――」", "と言った」"),
        ("立たされている。――尤も", "立たされている。尤も"),  # 文の切れ目のダッシュ
        ("――\n本文", "本文"),  # 記号だけの行は落とす
        ("、本文――\n次", "本文、\n次"),  # 行の頭の読点は落とし、終わりは残す
        # 空白
        ("\u3000孤独\u3000\u3000について  考える ", "孤独 について 考える"),  # 全角の空白も詰める
    ],
)
def test_speech_text(src, want):
    assert speech_text(src) == want


@pytest.mark.parametrize(
    "text",
    [
        "「さあ」と言った。『本』を読む。",  # かぎかっこはそのまま
        "働く人の割合は32.9%（労働安全衛生調査、2025年）",  # 統計の括弧は読む
        "其の人（父）は来た。",  # 1字の括弧は別名か分からない
        "彼（ナポレオン）",  # 1字の語のあとの説明
        "四月三十日（土曜）\n関さんと",  # 日付のあとの曜日
        "冉有（ぜんゆう）・季路（きろ）、",  # 読みの括弧（ひらがな）
        "割合（セクハラ・パワハラを含む）",
        "ふりて、\n雪ふる空に",  # 行の終わりの読点は原文のもの
        "1／2の人",  # 数の間の斜線
        "明治三十九年（一九〇六）の本",
        "一行目。\n二行目。",  # 改行は文の区切りなので残す
        "3/4の人",
        "ますます",
    ],
)
def test_speech_text_keeps(text):
    assert speech_text(text) == text


@pytest.mark.parametrize(
    "src,want",
    [
        # 新字新仮名の一節や語りでも、踊り字は開いて読む（modernize を通さない文）
        (
            "ぐん〳〵実行にうつして行く。二時間はかゝる。",
            "ぐんぐん実行にうつして行く。二時間はかかる。",
        ),
        ("しみ〴〵と思う", "しみじみと思う"),
        ("ます／＼", "ますます"),  # 横組みの代用表記（斜線の区切りにしない）
        ("こゝろ／本", "こころ、本"),
        ("人〻", "人々"),
        ("時〳〵", "時〳〵"),  # 開き方が決まらないものはそのまま
    ],
)
def test_speech_text_opens_repeat_marks(src, want):
    assert speech_text(src) == want


@pytest.mark.parametrize(
    "src",
    [
        "いよ〳〵三月になつた。「さうでせう」と彼は云つた。",
        "むしや〳〵食ふ。こゝろ。時〳〵。ハヽヽ。",
        "かへす〴〵も、たゞ。",
    ],
)
def test_speech_text_after_modernize_is_stable(src):
    once = modernize(src)
    assert speech_text(once) == speech_text(speech_text(once))
    assert speech_text(once) == speech_text(modernize(once))  # 二度開くことはない
    assert "ゝ" not in speech_text(once) and "ゞ" not in speech_text(once)


# --- 人が直した読み ---------------------------------------------------------------


def test_load_overrides_missing_file(tmp_path):
    assert load_overrides(tmp_path / "readings.csv") == {}


def test_load_overrides(tmp_path):
    path = tmp_path / "readings.csv"
    path.write_text(
        "key,reading,note\n"
        "ch2-s3,いよいよ三月になった,くの字点\n"
        ",,\n"
        " C-0001 , 東京へ帰ったらどうか ,\n",
        encoding="utf-8-sig",  # Excel で保存した BOM 付き
    )
    assert load_overrides(path) == {
        "ch2-s3": "いよいよ三月になった",
        "C-0001": "東京へ帰ったらどうか",
    }


def test_text_sha():
    import hashlib

    text = "いよ〳〵三月になつた。"
    assert text_sha(text) == hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    assert len(text_sha("")) == 8
    assert text_sha("あ") != text_sha("い")


def test_load_overrides_with_sha(tmp_path):
    sha = text_sha("いよ〳〵三月になつた。")
    old = text_sha("いよ〳〵二月になつた。")
    path = tmp_path / "readings.csv"
    path.write_text(
        "key,reading,note,sha\n"
        f"ch2-s3,いよいよ三月になった,くの字点,{sha}\n"
        f"ch2-s3,いよいよ二月になった,前の文のとき, {old.upper()} \n"  # sha が違えば別の行
        "C-0001,東京へ帰ったらどうか,,\n"  # sha が空なら文に関わらず使う
        ",,メモだけの行,\n",
        encoding="utf-8",
    )
    assert load_overrides(path) == {
        f"ch2-s3@{sha}": "いよいよ三月になった",
        f"ch2-s3@{old}": "いよいよ二月になった",  # 大文字・前後の空白はそろえる
        "C-0001": "東京へ帰ったらどうか",
    }


def test_override_for(tmp_path):
    display = "いよ〳〵三月になつた。"
    sha = text_sha(display)
    overrides = {
        f"ch2-s3@{sha}": "いよいよ三月になった",
        "ch2-s4@00000000": "古い文の読み",  # 文が変わったので使わない
        "C-0001": "東京へ帰ったらどうか",
        f"C-0002@{sha}": "文に結んだ読み",
        "C-0002": "文に結ばない読み",
    }
    assert override_for(overrides, "ch2-s3", display) == "いよいよ三月になった"
    assert override_for(overrides, "ch2-s3", display + "。") is None  # 文が変わった
    assert override_for(overrides, "ch2-s4", "新しい文") is None
    assert override_for(overrides, "C-0001", "どの文でも") == "東京へ帰ったらどうか"
    assert override_for(overrides, "C-0002", display) == "文に結んだ読み"  # sha の行が先
    assert override_for(overrides, "C-0002", "別の文") == "文に結ばない読み"
    assert override_for({}, "ch1-s1", display) is None


def test_load_overrides_then_override_for(tmp_path):
    display = "國の爲に"
    path = tmp_path / "readings.csv"
    path.write_text(
        f"key,reading,note,sha\nch1-s1,くにのために,,{text_sha(display)}\n", encoding="utf-8"
    )
    overrides = load_overrides(path)
    assert override_for(overrides, "ch1-s1", display) == "くにのために"
    assert override_for(overrides, "ch1-s1", "国の為に") is None  # 原文が直された


@pytest.mark.parametrize(
    "body,message",
    [
        ("key,reading,note\nch1-s1,よみ,\nch1-s1,べつのよみ,\n", "2回"),
        ("key,reading,note\nch1-s1,,\n", "両方"),
        ("key,note\nch1-s1,メモ\n", "reading"),
        ("key,reading,note,sha\nch1-s1,よみ,,abc\n", "sha"),  # 8字の16進でない
        ("key,reading,note,sha\nch1-s1,よみ,,0123456g\n", "sha"),
        ("key,reading,note,sha\nch1-s1,よみ,,01234567\nch1-s1,よみ2,,01234567\n", "2回"),
        ("key,reading,note,sha\n,よみ,,01234567\n", "両方"),
    ],
)
def test_load_overrides_rejects_unclear_rows(tmp_path, body, message):
    path = tmp_path / "readings.csv"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_overrides(path)


def test_word_readings_table(tmp_path):
    import pytest

    from koten.voice.reading import apply_word_readings, load_word_readings

    p = tmp_path / "reading_words.csv"
    assert load_word_readings(p) == []
    p.write_text("surface,reading,note\n方も,ほうも,\n感じる方も,感じるかたも,\n,,\n", "utf-8")
    words = load_word_readings(p)
    assert words[0][0] == "感じる方も"  # 長い表記が先
    assert apply_word_readings("そう感じる方もいる。こちらの方もよい。", words) == (
        "そう感じるかたもいる。こちらのほうもよい。"
    )
    p.write_text("surface,reading\n層む,\n", "utf-8")
    with pytest.raises(ValueError, match="両方"):
        load_word_readings(p)
    p.write_text("surface,reading\n層む,かさむ\n層む,そうむ\n", "utf-8")
    with pytest.raises(ValueError, match="2回"):
        load_word_readings(p)


def test_config_reading_words_loads():
    from pathlib import Path

    from koten.voice.reading import load_word_readings

    words = dict(load_word_readings(Path(__file__).parents[1] / "config" / "reading_words.csv"))
    assert words["層む"] == "かさむ" and words["奔って"] == "はしって"
