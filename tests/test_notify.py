"""텔레그램 알림 전달 — 요청 조립·그레이스풀(네트워크 없이 목킹)."""

from signal_desk import config, notify


def test_push_sends_to_all_chats(monkeypatch):
    sent = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        import json
        sent.append((req.full_url, json.loads(req.data.decode())))
        return _Resp()

    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    monkeypatch.setattr(config, "telegram_chat_ids", lambda: ["111", "222"])
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)

    assert notify.push("테스트 알림") is True
    assert len(sent) == 2
    assert "bot TOK/sendMessage".replace(" ", "") in sent[0][0].replace(" ", "")
    assert sent[0][1]["chat_id"] == "111" and sent[0][1]["text"] == "테스트 알림"


def test_push_noop_without_config(monkeypatch):
    monkeypatch.setattr(config, "telegram_token", lambda: None)
    assert notify.push("x") is False
    assert notify.available() is False


def test_push_graceful_on_error(monkeypatch):
    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    monkeypatch.setattr(config, "telegram_chat_ids", lambda: ["111"])

    def boom(req, timeout=None):
        raise OSError("network")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.push("x") is False  # 예외 삼키고 False


def test_push_reports_partial_failure_not_success(monkeypatch):
    monkeypatch.setattr(config, "telegram_token", lambda: "TOK")
    monkeypatch.setattr(config, "telegram_chat_ids", lambda: ["111", "222"])
    seen = []

    def send(token, chat, text):
        seen.append(chat)
        return chat == "111"

    monkeypatch.setattr(notify, "_send_one", send)
    assert notify.push("x") is False
    assert seen == ["111", "222"]
