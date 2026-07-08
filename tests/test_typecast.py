"""Typecast TTS 클라이언트 — 요청 조립·그레이스풀 폴백(네트워크 없이 목킹)."""

from signal_desk import config
from signal_desk.ingest import typecast


def test_synthesize_builds_request_and_returns_bytes(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"ID3_fake_mp3"

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["key"] = req.headers.get("X-api-key")
        import json
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(config, "typecast_key", lambda: "SECRET")
    monkeypatch.setattr(config, "typecast_voice_id", lambda: "tc_voice")
    monkeypatch.setattr(config, "typecast_model", lambda: "ssfm-v30")
    monkeypatch.setattr(typecast.urllib.request, "urlopen", fake_urlopen)

    out = typecast.synthesize("오늘의 시그널입니다.")
    assert out == b"ID3_fake_mp3"
    assert captured["url"] == "https://api.typecast.ai/v1/text-to-speech"
    assert captured["key"] == "SECRET"
    assert captured["body"]["voice_id"] == "tc_voice"
    assert captured["body"]["text"] == "오늘의 시그널입니다."
    assert captured["body"]["language"] == "kor"
    assert captured["body"]["output"]["audio_format"] == "mp3"


def test_synthesize_none_without_key(monkeypatch):
    monkeypatch.setattr(config, "typecast_key", lambda: None)
    assert typecast.synthesize("hi") is None
    assert typecast.available() is False


def test_synthesize_none_on_http_error(monkeypatch):
    import urllib.error
    monkeypatch.setattr(config, "typecast_key", lambda: "SECRET")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 402, "quota", {}, None)
    monkeypatch.setattr(typecast.urllib.request, "urlopen", boom)
    assert typecast.synthesize("hi") is None  # 그레이스풀
