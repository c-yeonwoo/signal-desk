"""토스 OAuth2 토큰 발급 실패 시 원인(HTTP 상태코드+응답본문)까지 로그에 남기는지 확인."""

import io
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
