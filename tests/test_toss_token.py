"""토스 OAuth2 토큰 발급 실패 시 원인(HTTP 상태코드+응답본문)까지 로그에 남기는지 확인."""

import io
import time
import urllib.error

from signal_desk.ingest import toss


def test_token_http_error_logs_detail(monkeypatch, caplog):
    monkeypatch.setenv("TOSS_CLIENT_ID", "id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "secret")
    toss._token["value"], toss._token["exp"] = None, 0.0

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            toss._BASE + "/oauth2/token", 401, "Unauthorized",
            hdrs=None, fp=io.BytesIO(b'{"error":"invalid_client"}'))

    monkeypatch.setattr(toss.urllib.request, "urlopen", fake_urlopen)
    with caplog.at_level("WARNING"):
        tok = toss._access_token()
    assert tok is None
    assert any("401" in r.message and "invalid_client" in r.message for r in caplog.records)


def test_no_creds_returns_none_silently(monkeypatch):
    monkeypatch.delenv("TOSS_CLIENT_ID", raising=False)
    monkeypatch.delenv("TOSS_CLIENT_SECRET", raising=False)
    toss._token["value"], toss._token["exp"] = None, 0.0
    assert toss._access_token() is None


def test_data_401_clears_token_and_retries(monkeypatch):
    """죽은 캐시 토큰으로 401 → 폐기·재발급·1회 재시도 성공."""
    monkeypatch.setenv("TOSS_CLIENT_ID", "id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "secret")
    toss._token["value"], toss._token["exp"] = "stale-token", time.time() + 3600
    calls: list[str] = []

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", None) or req.get_full_url()
        auth = req.headers.get("Authorization") or req.headers.get("authorization") or ""
        calls.append(url)
        if "/oauth2/token" in url:
            return _Resp(b'{"access_token":"fresh-token","expires_in":3600}')
        if "/api/v1/prices" in url:
            if "stale-token" in auth:
                raise urllib.error.HTTPError(
                    url, 401, "Unauthorized", hdrs=None,
                    fp=io.BytesIO(b'{"error":{"code":"invalid-token"}}'))
            return _Resp(b'{"result":[{"symbol":"005930","lastPrice":"70000"}]}')
        raise AssertionError(url)

    monkeypatch.setattr(toss.urllib.request, "urlopen", fake_urlopen)
    out = toss.prices(["005930"])
    assert out.get("005930") == 70000.0
    assert any("/oauth2/token" in u for u in calls)
    assert sum(1 for u in calls if "/api/v1/prices" in u) == 2


def test_data_401_retry_still_fails_logs_reissue_hint(monkeypatch, caplog):
    monkeypatch.setenv("TOSS_CLIENT_ID", "id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "secret")
    toss._token["value"], toss._token["exp"] = "stale", time.time() + 3600

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", None) or req.get_full_url()
        if "/oauth2/token" in url:
            return _Resp(b'{"access_token":"still-bad","expires_in":3600}')
        raise urllib.error.HTTPError(
            url, 401, "Unauthorized", hdrs=None,
            fp=io.BytesIO(b'{"error":{"code":"unauthorized"}}'))

    monkeypatch.setattr(toss.urllib.request, "urlopen", fake_urlopen)
    with caplog.at_level("WARNING"):
        assert toss.prices(["005930"]) == {}
    assert any("재발급 검토" in r.message for r in caplog.records)
