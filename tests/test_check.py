"""今の本（著作権あり）での照合（SPEC R8・D-017）：判定だけを残し、食い違いなら公開を保留する。"""

# ruff: noqa: E501

import json
import shutil
import sqlite3

import pytest
from typer.testing import CliRunner

import koten.llm
from koten.check import compare
from koten.check.books import BOOK_COLUMNS, BooksError, read_books
from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR, REPO_ROOT, load_settings
from koten.db import connect
from tests.m4_corpus import build
from tests.ref_fixture import write_csv
from tests.test_script import planned, run

BOOK_TEXT = "（訳文）うらやむ心を、人は努力の芽として見なおすことができる、と訳者は述べる。"
MARK = "見なおすことができる"  # 本の文が、どこにも保存されていないことを確かめる印
# 手元の Ollama の確かめ（下の local_ollama で差し替える前の、本物の関数）
MODEL_PROBLEMS = compare.model_problems
DEBUG_ENV_PROBLEMS = compare.debug_env_problems


def book_row(**kw):
    base = {
        "book_id": "B-gendai",
        "title": "架空の今の訳",
        "people": "今野訳者",
        "publisher": "架空書房",
        "year": "2001",
        "kind": "translation",
        "medium": "paper_owned",
    }
    return {**base, **kw}


class LocalJudge:
    """手元の Ollama の代わり。渡された本の文を覚え、決めたラベルを返す。"""

    name = "fake-ollama"

    def __init__(self, verdict="same"):
        self.verdict = verdict
        self.users: list[str] = []

    def complete(self, system, user, schema=None):
        self.users.append(user)
        assert schema["properties"]["verdict"]["enum"] == ["same", "differs", "not_covered"]
        return json.dumps({"verdict": self.verdict})


@pytest.fixture(autouse=True)
def local_ollama(monkeypatch):
    """手元の Ollama の確かめ（モデルの転送先・デバッグの設定）は、ここでは問題なしにする。"""
    monkeypatch.setattr(compare, "model_problems", lambda conf, client=None: [])
    monkeypatch.setattr(compare, "debug_env_problems", lambda getenv=None: [])


@pytest.fixture
def env(settings_file, tmp_path):
    """台本ができた回と、照合用の設定（今の本の台帳・照合のプロンプト・手元の AI）。"""
    s = load_settings(settings_file)
    build(s.require_hdd(), s.ssd_root, DEFAULT_CONFIG_DIR)
    eid = planned(s, "A", worry_id="W0001")
    r, _ = run(s, eid)
    assert r.state == "script_done", r.state_msg
    # 偽の台本作家は、どの章も同じ文で始める。保留は文の中身に付くので、文を見分けられるようにする
    conn = connect(s.ssd_root)
    script = json.loads(
        conn.execute("SELECT script_json FROM episodes WHERE episode_id = ?", (eid,)).fetchone()[0]
    )
    for ch in script["chapters"]:
        for seg in ch["segments"]:
            if seg["kind"] == "narration":
                seg["text"] = seg["text"].replace("。", f"（{seg['seg_id']}）。", 1)
    with conn:
        conn.execute(
            "UPDATE episodes SET script_json = ? WHERE episode_id = ?",
            (json.dumps(script, ensure_ascii=False), eid),
        )
    cfg = tmp_path / "cfg" / "config"
    cfg.mkdir(parents=True)
    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", cfg / "sources.yaml")
    shutil.copytree(REPO_ROOT / "prompts", tmp_path / "cfg" / "prompts")
    write_csv(cfg / "ref_books.csv", BOOK_COLUMNS, [book_row()])
    (cfg / "settings.toml").write_text(
        f'[paths]\nhdd_root = "{s.hdd_root}"\nssd_root = "{s.ssd_root}"\n'
        '[llm.check]\nprovider = "ollama"\nendpoint = "http://127.0.0.1:11434"\nmodel = "gemma4:26b"\n'
        '[check]\nprompt = "check_v1"\n',
        "utf-8",
    )
    return s, eid, ["--settings", str(cfg / "settings.toml")], cfg


def cli(st, *args, input=None):
    return CliRunner().invoke(app, [*args, *st], input=input)


def first_claim(st, eid):
    r = cli(st, "check", "list", "--episode", eid)
    assert r.exit_code == 0, r.output
    line = next(x for x in r.output.splitlines() if x.startswith("C"))
    return line.split("（")[0], line


def assert_not_stored(s, tmp_path):
    """本の文が、SSD の DB にも、SSD・HDD のファイルにも残っていない。"""
    conn = sqlite3.connect(s.ssd_root / "db" / "koten.sqlite")
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        for row in conn.execute(f"SELECT * FROM {table}"):
            assert not any(isinstance(v, str) and MARK in v for v in row), table
    for root in (s.ssd_root, s.hdd_root):
        for p in root.rglob("*"):
            if p.is_file():
                assert MARK.encode() not in p.read_bytes(), p


def test_same_changes_nothing(env, monkeypatch, tmp_path):
    s, eid, st, _ = env
    judge = LocalJudge("same")
    monkeypatch.setattr(compare, "local_llm", lambda conf: judge)
    number, line = first_claim(st, eid)
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "23ページ",
        input=BOOK_TEXT,
    )
    assert r.exit_code == 0, r.output
    assert "一致（内容が同じ）" in r.output and "何も変えません" in r.output
    assert BOOK_TEXT not in r.output  # 本の文を画面に出し直さない
    assert BOOK_TEXT in judge.users[0]  # 手元の AI には渡した
    assert cli(st, "check", "status", "--episode", eid).exit_code == 0
    row = connect(s.ssd_root).execute("SELECT * FROM checks").fetchone()
    assert (
        row["verdict"] == "same"
        and row["method"] == "ollama:gemma4:26b"
        and row["locator"] == "23ページ"
    )
    assert row["book_label"] == "今野訳者、『架空の今の訳』（架空書房、2001年）"
    assert_not_stored(s, tmp_path)


def test_differs_holds_until_rewritten_and_rechecked(env, monkeypatch, tmp_path):
    s, eid, st, _ = env
    monkeypatch.setattr(compare, "local_llm", lambda conf: LocalJudge("differs"))
    number, _ = first_claim(st, eid)
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "23ページ",
        input=BOOK_TEXT,
    )
    assert r.exit_code == 0, r.output
    assert "食い違い" in r.output and "公開を保留します" in r.output
    r = cli(st, "check", "status", "--episode", eid)
    assert r.exit_code == 1 and "内容が食い違う" in r.output
    assert "照合で公開保留" in cli(st, "episode", "list").output
    # 台本の文を直すと、同じ本でもう一度照合するまで保留のまま
    conn = connect(s.ssd_root)
    key = conn.execute("SELECT claim_key FROM checks").fetchone()[0]
    seg_id, k = key.split("#")
    script = json.loads(
        conn.execute("SELECT script_json FROM episodes WHERE episode_id = ?", (eid,)).fetchone()[0]
    )
    seg = next(x for ch in script["chapters"] for x in ch["segments"] if x["seg_id"] == seg_id)
    sents = seg["text"].split("。")
    sents[int(k) - 1] = "書き直した文です"
    seg["text"] = "。".join(sents)
    with conn:
        conn.execute(
            "UPDATE episodes SET script_json = ? WHERE episode_id = ?",
            (json.dumps(script, ensure_ascii=False), eid),
        )
    r = cli(st, "check", "status", "--episode", eid)
    assert r.exit_code == 1 and "同じ本でもう一度照合する" in r.output
    monkeypatch.setattr(compare, "local_llm", lambda conf: LocalJudge("same"))
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "23ページ",
        input=BOOK_TEXT,
    )
    assert r.exit_code == 0 and "何も変えません" in r.output, r.output
    assert cli(st, "check", "status", "--episode", eid).exit_code == 0
    assert_not_stored(s, tmp_path)


def test_close_wording_holds_even_when_same(env, monkeypatch):
    s, eid, st, _ = env
    monkeypatch.setattr(compare, "local_llm", lambda conf: LocalJudge("same"))
    number, line = first_claim(st, eid)
    claim_text = line.split("）", 1)[1]
    # 台本の文が本の言い回しとほとんど同じ（今の訳を写したような文）
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "5ページ",
        input="本では" + claim_text,
    )
    assert r.exit_code == 0, r.output
    assert "言い回しが本に近い" in r.output and "公開を保留します" in r.output
    r = cli(st, "check", "status", "--episode", eid)
    assert r.exit_code == 1 and "言い回しが本に近い" in r.output


def test_human_verdict_and_resolve_for_removed_sentence(env):
    s, eid, st, _ = env
    number, _ = first_claim(st, eid)
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "9ページ",
        "--verdict",
        "differs",
        "--note",
        "解釈が逆",
    )
    assert r.exit_code == 0 and "公開を保留します" in r.output, r.output
    conn = connect(s.ssd_root)
    row = conn.execute("SELECT * FROM checks").fetchone()
    assert row["method"] == "human" and row["note"] == "解釈が逆"
    key = row["claim_key"]
    # 文が残っているあいだは resolve で解けない
    r = cli(st, "check", "resolve", "--episode", eid, "--key", key, "--note", "直した")
    assert r.exit_code == 2 and "まだ台本にあります" in r.output
    # 段落ごと消すと、理由を書いて解ける
    seg_id = key.split("#")[0]
    script = json.loads(
        conn.execute("SELECT script_json FROM episodes WHERE episode_id = ?", (eid,)).fetchone()[0]
    )
    for ch in script["chapters"]:
        ch["segments"] = [x for x in ch["segments"] if x["seg_id"] != seg_id]
    with conn:
        conn.execute(
            "UPDATE episodes SET script_json = ? WHERE episode_id = ?",
            (json.dumps(script, ensure_ascii=False), eid),
        )
    r = cli(st, "check", "status", "--episode", eid)
    assert r.exit_code == 1 and "台本から消えた" in r.output
    r = cli(st, "check", "resolve", "--episode", eid, "--key", key, "--note", "段落を削った")
    assert r.exit_code == 0, r.output
    assert cli(st, "check", "status", "--episode", eid).exit_code == 0


def test_cloud_llm_is_refused(env, monkeypatch):
    s, eid, st, cfg = env
    (cfg / "settings.toml").write_text(
        f'[paths]\nhdd_root = "{s.hdd_root}"\nssd_root = "{s.ssd_root}"\n'
        '[llm.check]\nprovider = "claude_cli"\nmodel = "haiku"\n',
        "utf-8",
    )
    monkeypatch.setattr(
        koten.llm, "make_llm", lambda conf, model=None: pytest.fail("AI を呼んではいけない")
    )
    number, _ = first_claim(st, eid)
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "1ページ",
        input=BOOK_TEXT,
    )
    assert r.exit_code == 2 and "手元の Ollama" in r.output
    assert connect(s.ssd_root).execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 0


def test_book_text_length_and_unknown_book(env, monkeypatch):
    s, eid, st, _ = env
    monkeypatch.setattr(compare, "local_llm", lambda conf: LocalJudge())
    number, _ = first_claim(st, eid)
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "1",
        input="短い",
    )
    assert r.exit_code == 2 and "10〜1500字" in r.output
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "1",
        input="あ" * 1501,
    )
    assert r.exit_code == 2
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-none",
        "--page",
        "1",
        input=BOOK_TEXT,
    )
    assert r.exit_code == 2 and "台帳" in r.output


def test_ask_failure_does_not_leak_book_text():
    class Broken:
        name = "broken"

        def complete(self, system, user, schema=None):
            raise koten.llm.LLMError(f"失敗：{user}")

    with pytest.raises(compare.CheckError) as e:
        compare.ask(Broken(), "s", "{book}", "主張", "段落", BOOK_TEXT)
    assert MARK not in str(e.value)


def test_require_local():
    compare.require_local({"provider": "ollama", "endpoint": "http://127.0.0.1:11434"})
    compare.require_local({"provider": "ollama", "endpoint": "http://localhost:11434/v1"})
    for conf in (
        {"provider": "ollama", "endpoint": "http://192.168.1.5:11434"},
        {"provider": "ollama", "endpoint": "https://ollama.example.com"},
        {"provider": "claude_cli", "model": "haiku"},
        {"provider": "openai_compatible", "endpoint": "http://127.0.0.1:1234/v1"},
    ):
        with pytest.raises(compare.CheckError):
            compare.require_local(conf)


def test_wording_screen():
    run_len, share = compare.wording(
        "うらやむ心は努力の芽である", "（訳）うらやむ心は努力の芽である、と言う。"
    )
    assert run_len == 13 and share == 1.0 and compare.is_close(run_len, share)
    run_len, share = compare.wording("ねたみは自分を見つめる鏡だ", "うらやむ心は努力の芽である")
    assert not compare.is_close(run_len, share)


@pytest.mark.parametrize(
    "rows,cols,message",
    [
        ([book_row(medium="ebook")], BOOK_COLUMNS, "紙の本"),
        ([book_row(note="あ" * 61)], BOOK_COLUMNS, "60字まで"),
        ([book_row(book_id="x")], BOOK_COLUMNS, "B- で始め"),
        ([book_row(), book_row()], BOOK_COLUMNS, "重複"),
        ([{**book_row(), "text": "本の文"}], BOOK_COLUMNS + ["text"], "決まっていない列"),
    ],
)
def test_book_registry_rules(tmp_path, rows, cols, message):
    write_csv(tmp_path / "ref_books.csv", cols, rows)
    with pytest.raises(BooksError, match=message):
        read_books(tmp_path)


def _show(payload, status=200):
    import httpx

    def handler(request):
        assert request.url.path == "/api/show"
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_model_problems_refuses_cloud_models():
    conf = {"provider": "ollama", "endpoint": "http://127.0.0.1:11434", "model": "gemma4:26b"}
    assert MODEL_PROBLEMS(conf, _show({"details": {}})) == []
    assert "クラウド" in MODEL_PROBLEMS(conf, _show({"remote_host": "https://ollama.com:443"}))[0]
    assert "無い" in MODEL_PROBLEMS(conf, _show({}, status=404))[0]
    assert ":cloud" in MODEL_PROBLEMS({**conf, "model": "gpt-oss:120b-cloud"})[0]
    assert ":cloud" in MODEL_PROBLEMS({**conf, "model": "qwen3:cloud"})[0]


def test_debug_env_problems():
    env = {"OLLAMA_DEBUG_LOG_REQUESTS": "1", "OLLAMA_DEBUG": "2"}
    found = DEBUG_ENV_PROBLEMS(lambda n: env.get(n, ""))
    assert any("OLLAMA_DEBUG_LOG_REQUESTS" in x for x in found)
    assert any("OLLAMA_DEBUG=2" in x for x in found)
    assert DEBUG_ENV_PROBLEMS(lambda n: {"OLLAMA_DEBUG": "1"}.get(n, "")) == []


def test_read_pasted_from_pipe():
    import io

    assert compare.read_pasted(io.StringIO("  本の文です。\n")) == "本の文です。"


def _script(conn, eid):
    return json.loads(
        conn.execute("SELECT script_json FROM episodes WHERE episode_id = ?", (eid,)).fetchone()[0]
    )


def _save_script(conn, eid, script):
    with conn:
        conn.execute(
            "UPDATE episodes SET script_json = ? WHERE episode_id = ?",
            (json.dumps(script, ensure_ascii=False), eid),
        )


def test_hold_follows_the_sentence_when_it_moves(env, monkeypatch):
    """食い違った文が、そのまま別の位置に動いても保留は解けない（位置ではなく中身に付く）。"""
    s, eid, st, _ = env
    number, _ = first_claim(st, eid)
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "9",
        "--verdict",
        "differs",
    )
    assert r.exit_code == 0, r.output
    conn = connect(s.ssd_root)
    key = conn.execute("SELECT claim_key FROM checks").fetchone()[0]
    seg_id = key.split("#")[0]
    script = _script(conn, eid)
    seg = next(x for ch in script["chapters"] for x in ch["segments"] if x["seg_id"] == seg_id)
    seg["text"] = "新しく足した文です。" + seg["text"]  # 食い違った文は2文目に動いた
    _save_script(conn, eid, script)
    # 1文目（新しい文）を照合して一致にしても、動いた文が残っているので保留のまま
    monkeypatch.setattr(compare, "local_llm", lambda conf: LocalJudge("same"))
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "9",
        input=BOOK_TEXT,
    )
    assert r.exit_code == 0, r.output
    r = cli(st, "check", "status", "--episode", eid)
    assert r.exit_code == 1 and f"{seg_id}#2" in r.output and "内容が食い違う" in r.output
    # resolve でも解けない
    r = cli(st, "check", "resolve", "--episode", eid, "--key", key, "--note", "x")
    assert r.exit_code == 2


def test_human_same_on_unchanged_sentence_keeps_the_hold(env, monkeypatch):
    s, eid, st, _ = env
    monkeypatch.setattr(compare, "local_llm", lambda conf: LocalJudge("same"))
    number, line = first_claim(st, eid)
    claim_text = line.split("）", 1)[1]
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "5",
        input="本では" + claim_text,
    )
    assert "言い回しが本に近い" in r.output
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "5",
        "--verdict",
        "same",
    )
    assert r.exit_code == 0
    assert cli(st, "check", "status", "--episode", eid).exit_code == 1  # 文を直すまで解けない
    assert "照合で公開保留" in cli(st, "episode", "list").output


def test_close_flag_only_makes_it_stricter(env, monkeypatch):
    s, eid, st, _ = env
    monkeypatch.setattr(compare, "local_llm", lambda conf: LocalJudge("same"))
    number, _ = first_claim(st, eid)
    r = cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "5",
        "--close",
        input=BOOK_TEXT,
    )
    assert r.exit_code == 0 and "言い回しが本に近い" in r.output
    assert connect(s.ssd_root).execute("SELECT close_wording FROM checks").fetchone()[0] == 1


def test_episode_list_flag_clears_after_recheck(env, monkeypatch):
    s, eid, st, _ = env
    monkeypatch.setattr(compare, "local_llm", lambda conf: LocalJudge("differs"))
    number, _ = first_claim(st, eid)
    cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "1",
        input=BOOK_TEXT,
    )
    assert "照合で公開保留" in cli(st, "episode", "list").output
    conn = connect(s.ssd_root)
    key = conn.execute("SELECT claim_key FROM checks").fetchone()[0]
    script = _script(conn, eid)
    seg = next(
        x for ch in script["chapters"] for x in ch["segments"] if x["seg_id"] == key.split("#")[0]
    )
    seg["text"] = (
        "書き直した文です。" + seg["text"].split("。", 1)[1] if key.endswith("#1") else seg["text"]
    )
    _save_script(conn, eid, script)
    monkeypatch.setattr(compare, "local_llm", lambda conf: LocalJudge("same"))
    cli(
        st,
        "check",
        "run",
        "--episode",
        eid,
        "--claim",
        number,
        "--book",
        "B-gendai",
        "--page",
        "1",
        input=BOOK_TEXT,
    )
    assert cli(st, "check", "status", "--episode", eid).exit_code == 0
    assert "照合で公開保留" not in cli(st, "episode", "list").output


def test_wording_counts_only_our_own_words():
    """DB から差し込んだ保護期間満了の訓読が今の本と同じでも、自分たちの言い回しは近くない。"""
    kundoku = "子曰く、学びて時に之を習ふ、亦説ばしからずや"
    book = "子曰く、学びて時に之を習う、亦た説ばしからずや。これは学ぶ喜びを述べたものである。"
    run_len, share = compare.wording("明治の訓読では、「\x00」と読みます。", book)
    assert not compare.is_close(run_len, share)
    run_len, share = compare.wording(f"明治の訓読では、「{kundoku}」と読みます。", book)
    assert compare.is_close(run_len, share)  # 区切らないと近いことになってしまう


def test_local_client_ignores_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    client = compare.local_client({"timeout_sec": 5})
    assert client._trust_env is False
    llm = compare.local_llm({"endpoint": "http://127.0.0.1:11434", "model": "gemma4:26b"})
    assert llm.client._trust_env is False
