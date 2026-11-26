"""net.py：秘密の引数（e-Stat の appId）は送るだけで、控え・ログ・エラーに残さない。"""

from __future__ import annotations

import json

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
    assert e.value.__cause__ is None
    assert e.value.__suppress_context__


def test_connection_error_text_has_no_secret(tmp_path):
    def server(req):
        raise httpx.ConnectError(f"失敗 {req.url}", request=req)

    f = make_fetcher(tmp_path, server)
    with pytest.raises(FetchError) as e:
        f.fetch("https://api.e-stat.go.jp/x", "x.json", secret_params={"appId": SECRET})
    assert SECRET not in str(e.value)
    assert "***" in str(e.value)


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
