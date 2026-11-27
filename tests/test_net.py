"""net.py：秘密の引数（e-Stat の appId）は送るだけで、控え・ログ・エラーに残さない。"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from koten.net import ApiError, Fetcher, FetchError

SECRET = "abcdef0123456789abcdef0123456789abcdef01"  # 40 字の偽の ID


def make_fetcher(tmp_path, server):
    client = httpx.Client(transport=httpx.MockTransport(server))
    return Fetcher(tmp_path, user_agent="test-agent", per_host_per_sec=0, client=client)


def test_secret_param_is_sent_but_never_stored(tmp_path):
    seen = []

    def server(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"echo": str(req.url)})

    f = make_fetcher(tmp_path, server)
    data = f.fetch_json(
        "https://api.e-stat.go.jp/rest/3.0/app/json/getMetaInfo?statsDataId=1",
        "estat",
        secret_params={"appId": SECRET},
    )
    assert "appId=" + SECRET in seen[0]
    assert "statsDataId=1" in seen[0]
    assert SECRET not in json.dumps(data)
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert SECRET not in p.read_text("utf-8", errors="ignore")
            assert SECRET not in p.name


def test_http_error_text_has_no_secret(tmp_path):
    f = make_fetcher(tmp_path, lambda req: httpx.Response(403, text="forbidden"))
    with pytest.raises(FetchError) as e:
        f.fetch("https://api.e-stat.go.jp/x?y=1", "x.json", secret_params={"appId": SECRET})
    assert SECRET not in str(e.value)
    assert SECRET not in repr(e.value)
    assert "403" in str(e.value) and "y=1" in str(e.value)
    assert e.value.status_code == 403
    assert e.value.__cause__ is None
    assert e.value.__suppress_context__


def test_connection_error_text_has_no_secret(tmp_path):
    def server(req):
        raise httpx.ConnectError(f"失敗 {req.url}", request=req)

    f = make_fetcher(tmp_path, server)
    with pytest.raises(FetchError) as e:
        f.fetch("https://api.e-stat.go.jp/x", "x.json", secret_params={"appId": SECRET})
    assert SECRET not in str(e.value)
    assert e.value.status_code is None


def test_without_secret_errors_are_unchanged(tmp_path):
    f = make_fetcher(tmp_path, lambda req: httpx.Response(403, text="forbidden"))
    with pytest.raises(httpx.HTTPStatusError):
        f.fetch("https://example.com/x", "x.json")
    assert issubclass(FetchError, httpx.HTTPError)


def test_error_of_drops_the_cached_answer(tmp_path):
    f = make_fetcher(tmp_path, lambda req: httpx.Response(200, json={"S": 100}))
    with pytest.raises(ApiError):
        f.fetch_json(
            "https://api.e-stat.go.jp/x",
            "estat",
            error_of=lambda d: "認証に失敗" if d["S"] >= 100 else None,
        )
    assert not list((tmp_path / "estat").glob("*.json"))


def test_mediawiki_error_still_detected_without_error_of(tmp_path):
    f = make_fetcher(tmp_path, lambda req: httpx.Response(200, json={"error": "bad"}))
    with pytest.raises(ApiError):
        f.fetch_json("https://example.com/api", "wiki")
    assert not list((tmp_path / "wiki").glob("*.json"))


def test_manifest_has_public_url_only(tmp_path):
    f = make_fetcher(tmp_path, lambda req: httpx.Response(200, json={"ok": 1}))
    url = "https://api.e-stat.go.jp/x?y=1"
    f.fetch_json(url, "estat", secret_params={"appId": SECRET})
    entry = f.manifest.get(url)
    assert entry is not None and SECRET not in json.dumps(entry.__dict__)


def test_refresh_is_passed_through(tmp_path):
    calls = []

    def server(req):
        calls.append(1)
        return httpx.Response(200, json={"n": len(calls)})

    f = make_fetcher(tmp_path, server)
    url = "https://api.e-stat.go.jp/x"
    f.fetch_json(url, "estat")
    f.fetch_json(url, "estat")
    assert len(calls) == 1
    f.fetch_json(url, "estat", refresh=True)
    assert len(calls) == 2


def test_no_secret_in_logs(tmp_path, caplog):
    f = make_fetcher(tmp_path, lambda req: httpx.Response(200, json={"ok": 1}))
    with caplog.at_level(logging.DEBUG):
        f.fetch_json("https://api.e-stat.go.jp/x?y=1", "estat", secret_params={"appId": SECRET})
    assert SECRET not in caplog.text
    # httpx の水準を下げられても、秘密は伏せる
    with caplog.at_level(logging.DEBUG, logger="httpx"):
        f.fetch_json("https://api.e-stat.go.jp/x?y=2", "estat", secret_params={"appId": SECRET})
    assert SECRET not in caplog.text
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_non_json_answer_is_not_kept(tmp_path):
    calls = []

    def server(req):
        calls.append(1)
        return httpx.Response(200, text="<html>メンテナンス中</html>")

    f = make_fetcher(tmp_path, server)
    for n in (1, 2):
        with pytest.raises(ValueError):
            f.fetch_json("https://api.e-stat.go.jp/x", "estat")
        assert not list((tmp_path / "estat").glob("*.json"))
        assert len(calls) == n


def test_error_of_raising_drops_the_cache(tmp_path):
    calls = []

    def server(req):
        calls.append(1)
        return httpx.Response(200, json={"a": 1})

    f = make_fetcher(tmp_path, server)
    for n in (1, 2):
        with pytest.raises(KeyError):
            f.fetch_json("https://api.e-stat.go.jp/x", "estat", error_of=lambda d: d["S"])
        assert not list((tmp_path / "estat").glob("*.json"))
        assert len(calls) == n


# --- 最後の見直しの直し（F7・S1） -------------------------------------------------------


def test_validate_refuses_a_bad_body_and_keeps_the_good_file(tmp_path):
    bodies = [b"a,b\n1,2\n", b"<!DOCTYPE html><html>maintenance</html>"]

    def server(req):
        return httpx.Response(200, content=bodies[0])

    f = make_fetcher(tmp_path, server)

    def check(body):
        return "HTML が返った" if body.lstrip().startswith(b"<") else None

    url = "https://www.e-stat.go.jp/stat-search/file-download?statInfId=1&fileKind=0"
    path, got = f.fetch(url, "files/1_0", validate=check)
    assert got and path.read_bytes() == bodies[0]
    entry = f.manifest.get(url)
    bodies[0] = bodies[1]
    with pytest.raises(ApiError, match="HTML が返った"):
        f.fetch(url, "files/1_0", refresh=True, validate=check)
    assert path.read_bytes() == b"a,b\n1,2\n" and f.manifest.get(url) == entry
    assert not list(tmp_path.rglob("*.part"))
    # はじめての取得でも、だめな中身は置かない
    with pytest.raises(ApiError):
        f.fetch(url + "&x=1", "files/2_0", validate=check)
    assert not (tmp_path / "files" / "2_0").exists()


@pytest.mark.parametrize(
    "name",
    ["httpcore", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy",
     "httpcore.socks"],
)  # fmt: skip
def test_httpcore_logs_are_redacted(tmp_path, caplog, name):
    f = make_fetcher(tmp_path, lambda req: httpx.Response(200, json={"ok": 1}))
    f.fetch_json("https://api.e-stat.go.jp/x?y=1", "estat", secret_params={"appId": SECRET})
    with caplog.at_level(logging.DEBUG, logger=name):
        logging.getLogger(name).debug(
            "receive_response_headers.complete return_value=%s",
            [(b"Location", f"/b?appId={SECRET}".encode())],
        )
    assert caplog.records and SECRET not in caplog.text
    assert all(SECRET not in r.getMessage() for r in caplog.records)
