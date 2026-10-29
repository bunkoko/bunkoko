"""国会図書館の資料を一度だけまとめて取る（M8a・D-003）。偽の NDL で確かめる。"""

# ruff: noqa: E501

import json
import shutil

import httpx
import pytest
from typer.testing import CliRunner

from koten.cli import app
from koten.config import DEFAULT_CONFIG_DIR
from koten.net import Fetcher, RateLimiter
from koten.ref.ndl import (
    NdlError,
    access_kind,
    access_value,
    parse_koma,
    snapshot_path,
    take_snapshot,
)
from koten.ref.registry import read_registry
from tests.ref_fixture import person, work, write, write_texts

PID = "000001"
NDL = {"source": "ndl", "pid": PID, "ndl_access": "internet_pd", "ndl_checked_at": "2026-10-29"}


def manifest(value="PDM", koma=5, label="Access Restrictions"):
    return {
        "@context": "http://iiif.io/api/presentation/2/context.json",
        "metadata": [{"label": "Title", "value": "架空辞書"}, {"label": label, "value": value}],
        "sequences": [{"canvases": [{"@id": f"c{i}"} for i in range(1, koma + 1)]}],
    }


class FakeNdl:
    def __init__(self, man=None, block_after=None):
        self.man = man or manifest()
        self.calls: list[str] = []
        self.block_after = block_after

    def __call__(self, request):
        url = str(request.url)
        self.calls.append(url)
        if url == f"https://dl.ndl.go.jp/api/iiif/{PID}/manifest.json":
            return httpx.Response(200, json=self.man)
        images = [c for c in self.calls if "/R0" in c]
        if "/R0" in url:
            if self.block_after is not None and len(images) > self.block_after:
                return httpx.Response(403)
            return httpx.Response(200, content=b"jpeg" + url.encode())
        return httpx.Response(404)


def fetcher(raw, server):
    client = httpx.Client(transport=httpx.MockTransport(server))
    return Fetcher(raw, user_agent="test", per_host_per_sec=1000, client=client)


@pytest.fixture
def ndl_work(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    write(cfg, [work("R-dict", **NDL)], [person("R-dict", 1, "大槻架空", "author", 1928)])
    return cfg, {w.ref_id: w for w in read_registry(cfg)}["R-dict"]


def test_access_value_and_kind():
    assert access_value(manifest("PDM")) == "PDM"
    assert (
        access_value(manifest("インターネット公開（保護期間満了）", label="公開範囲"))
        == "インターネット公開（保護期間満了）"
    )
    assert (
        access_value(
            {
                "metadata": [
                    {"label": {"@value": "Access Restrictions"}, "value": [{"@value": "PDM"}]}
                ]
            }
        )
        == "PDM"
    )
    assert access_value({"metadata": []}) == ""
    assert access_kind("PDM") == "internet_pd"
    assert access_kind("インターネット公開（保護期間満了）") == "internet_pd"
    for v in (
        "インターネット公開（裁定）",
        "インターネット公開（許諾）",
        "国立国会図書館内限定",
        "",
        "CC BY",
    ):
        assert access_kind(v) == "unknown"


def test_parse_koma():
    assert parse_koma("1-3,5", 10) == [1, 2, 3, 5]
    assert parse_koma("all", 3) == [1, 2, 3]
    for bad in ("0", "4-2", "11", "a"):
        with pytest.raises(NdlError):
            parse_koma(bad, 10)


def test_snapshot_fetches_manifest_and_chosen_koma_once(ndl_work, tmp_path):
    _, w = ndl_work
    hdd = tmp_path / "hdd"
    server = FakeNdl()
    snap = take_snapshot(hdd, w, fetcher(hdd / "raw" / "ndl", server), koma="2-3")
    assert snap.access == "internet_pd" and snap.koma_total == 5 and snap.images == [2, 3]
    assert (hdd / "raw/ndl" / PID / "images/0002.jpg").exists()
    assert f"https://dl.ndl.go.jp/api/iiif/{PID}/R0000002/full/full/0/default.jpg" in server.calls
    saved = json.loads(snapshot_path(hdd, PID).read_text("utf-8"))
    assert saved["images"] == [2, 3] and saved["access_value"] == "PDM"
    first = saved["access_checked_at"]
    # 2回目は控えを使い、取りに行かない（一度だけ）
    again = FakeNdl()
    snap = take_snapshot(hdd, w, fetcher(hdd / "raw" / "ndl", again), koma="2-3")
    assert again.calls == [] and snap.images == [2, 3]
    assert snap.access_checked_at == first and snap.first_run_at == saved["first_run_at"]
    # 続きを違う大きさで取ると、混ざるので止める
    with pytest.raises(NdlError, match="同じ大きさ"):
        take_snapshot(hdd, w, fetcher(hdd / "raw" / "ndl", FakeNdl()), koma="4", size="2000,")


def test_not_public_domain_manifest_stops_images(ndl_work, tmp_path):
    _, w = ndl_work
    hdd = tmp_path / "hdd"
    server = FakeNdl(manifest("インターネット公開（許諾）"))
    snap = take_snapshot(hdd, w, fetcher(hdd / "raw" / "ndl", server), koma="all")
    assert snap.access == "unknown" and snap.images == [] and "画像は取らない" in snap.stopped
    assert not any("/R0" in c for c in server.calls)


def test_403_stops_and_resumes(ndl_work, tmp_path):
    _, w = ndl_work
    hdd = tmp_path / "hdd"
    snap = take_snapshot(hdd, w, fetcher(hdd / "raw" / "ndl", FakeNdl(block_after=2)), koma="all")
    assert snap.images == [1, 2] and "403" in snap.stopped
    snap = take_snapshot(hdd, w, fetcher(hdd / "raw" / "ndl", FakeNdl()), koma="all")
    assert snap.images == [1, 2, 3, 4, 5] and not snap.stopped


def test_unusable_or_non_ndl_work_is_refused(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    write(
        cfg,
        [work("R-kojin", **{**NDL, "ndl_access": "kojin"}), work("R-own", source="owned")],
        [person("R-kojin", 1, "甲", "author", 1928), person("R-own", 1, "乙", "author", 1928)],
    )
    works = {w.ref_id: w for w in read_registry(cfg)}
    server = FakeNdl()
    with pytest.raises(NdlError, match="取らない"):
        take_snapshot(tmp_path, works["R-kojin"], fetcher(tmp_path / "raw", server))
    with pytest.raises(NdlError, match="NDL の資料ではない"):
        take_snapshot(tmp_path, works["R-own"], fetcher(tmp_path / "raw", server))
    assert server.calls == []


def test_rate_limiter_host_intervals():
    now = [0.0]
    slept: list[float] = []
    rl = RateLimiter(
        1000, clock=lambda: now[0], sleep=slept.append, host_intervals={"ndl.go.jp": 3.0}
    )
    rl.wait("https://dl.ndl.go.jp/a")
    now[0] = 1.0
    rl.wait("https://dl.ndl.go.jp/b")
    assert slept == [2.0]
    rl.wait("https://raw.githubusercontent.com/a")
    now[0] = 1.0005
    rl.wait("https://raw.githubusercontent.com/b")
    assert slept[-1] < 0.01  # 国会図書館以外はふだんの間隔
    assert rl.interval("lab.ndl.go.jp") == 3.0 and rl.interval("notndl.go.jp") < 1


def test_cli_is_off_until_terms_are_confirmed(tmp_path, ndl_work, monkeypatch):
    import koten.cli as cli

    cfg, _ = ndl_work
    shutil.copy(DEFAULT_CONFIG_DIR / "sources.yaml", cfg / "sources.yaml")
    write_texts(cfg)
    settings = cfg / "settings.toml"
    base = f'[paths]\nhdd_root = "{tmp_path / "hdd"}"\nssd_root = "{tmp_path / "ssd"}"\n'
    settings.write_text(base, "utf-8")
    st = ["--settings", str(settings)]
    r = CliRunner().invoke(app, ["ref", "ndl-snapshot", "--ref", "R-dict", *st])
    assert r.exit_code == 2 and "fetch_enabled" in r.output
    settings.write_text(base + "[ndl]\nfetch_enabled = true\n", "utf-8")
    monkeypatch.setattr(
        cli,
        "_make_fetcher",
        lambda s, raw: fetcher(raw, FakeNdl(manifest("インターネット公開（裁定）"))),
    )
    r = CliRunner().invoke(app, ["ref", "ndl-snapshot", "--ref", "R-dict", "--koma", "1-2", *st])
    assert r.exit_code == 1 and "保護期間満了ではない" in r.output
    # 台帳では internet_pd なのに、マニフェストは違う → その資料は使わない（SSD に写した判定で）
    from koten.db import connect

    r = CliRunner().invoke(app, ["ref", "build", *st])
    assert r.exit_code == 0 and "この資料は使いません" in r.output, r.output
    conn = connect(tmp_path / "ssd")
    assert (
        conn.execute("SELECT license FROM ref_works WHERE ref_id = 'R-dict'").fetchone()[0]
        == "UNKNOWN"
    )
    # HDD が無くても、SSD に写したマニフェストの判定で使わない
    shutil.rmtree(tmp_path / "hdd")
    settings.write_text(
        base.replace(str(tmp_path / "hdd"), "/no/such/volume/koten")
        + "[ndl]\nfetch_enabled = true\n",
        "utf-8",
    )
    r = CliRunner().invoke(app, ["ref", "build", *st])
    assert r.exit_code == 0 and "この資料は使いません" in r.output, r.output
    assert (
        conn.execute("SELECT license FROM ref_works WHERE ref_id = 'R-dict'").fetchone()[0]
        == "UNKNOWN"
    )
    # ref check でも使えないと出る
    r = CliRunner().invoke(app, ["ref", "check", *st])
    assert "R-dict" in r.output and "使えない" in r.output and "マニフェスト" in r.output


def test_unusable_ndl_images_are_forgotten(tmp_path):
    from koten.ref.standardebooks import forget_unusable

    cfg = tmp_path / "config"
    cfg.mkdir()
    write(cfg, [work("R-dict", **NDL)], [person("R-dict", 1, "大槻架空", "author", 1928)])
    works = {w.ref_id: w for w in read_registry(cfg)}
    hdd = tmp_path / "hdd"
    take_snapshot(hdd, works["R-dict"], fetcher(hdd / "raw" / "ndl", FakeNdl()), koma="1-2")
    images = hdd / "raw" / "ndl" / PID / "images"
    assert images.exists()
    assert forget_unusable(hdd, works, {}) == []  # 使えるうちは消さない
    # マニフェストの公開範囲で使えないと分かった
    removed = forget_unusable(hdd, works, {}, {"R-dict"})
    assert not images.exists() and removed == [f"国会図書館の画像 PID {PID}（公開範囲）"]
