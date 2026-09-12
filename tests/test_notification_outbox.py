from signal_desk import db, notify


def test_outbox_is_deduplicated_and_retries_after_delivery_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert notify.enqueue("important", dedupe_key="trade:1", now=100) is True
    assert notify.enqueue("important", dedupe_key="trade:1", now=100) is False

    monkeypatch.setattr(notify, "available", lambda: True)
    monkeypatch.setattr(notify, "push", lambda text: False)
    assert notify.drain(now=100)["failed"] == 1
    # first failure backs off 30 seconds; it cannot hammer Telegram on each 5-minute tick.
    assert notify.drain(now=129)["failed"] == 0

    monkeypatch.setattr(notify, "push", lambda text: True)
    assert notify.drain(now=130)["sent"] == 1
    assert db.notification_outbox_due(now=999) == []


def test_outbox_expires_before_delivery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert notify.enqueue("stale", dedupe_key="signal:old", expires_at=99, now=98) is True
    monkeypatch.setattr(notify, "available", lambda: True)
    monkeypatch.setattr(notify, "push", lambda text: (_ for _ in ()).throw(AssertionError("must not send")))

    assert notify.drain(now=100)["expired"] == 1


def test_outbox_health_reports_pending_delivery_without_message_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    notify.enqueue("do not expose", dedupe_key="health:pending", now=100)
    health = db.notification_outbox_health(now=100)

    assert health["pending"] == health["due"] == 1
    assert health["oldest_pending_ts"] == 100
    assert "do not expose" not in str(health)


def test_intraday_quote_and_execution_event_are_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert db.intraday_quotes_record("kr", {"005930": 70000}, ts=10) == 1
    assert db.intraday_quotes_record("kr", {"005930": 70000}, ts=10) == 0
    assert db.intraday_quotes_list("kr", "005930") == [{"ts": 10, "price": 70000.0}]

    assert db.execution_event_add("trade:kr:1", uid=1, market="kr", ticker="005930",
                                  event_type="filled_buy", price=70000, ts=10) is True
    assert db.execution_event_add("trade:kr:1", uid=1, market="kr", ticker="005930",
                                  event_type="filled_buy", price=70000, ts=10) is False
