"""알림(#16) — 관심종목 시그널 변동 알림 CRUD·상태 전이."""

from signal_desk import db


def test_alert_crud(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert db.alerts_unread(1) == 0 and db.alerts_list(1) == []

    # 최초 관측: 상태만 기록(알림 없음)
    db.alert_state_set(1, "005930", "HOLD")
    assert db.alert_state_all(1) == {"005930": "HOLD"}
    assert db.alerts_unread(1) == 0

    # 변동 발생 → 알림 추가
    db.alert_add(1, "005930", "삼성전자", "시그널 관망 → 매수")
    db.alert_state_set(1, "005930", "BUY")
    assert db.alert_state_all(1)["005930"] == "BUY"
    assert db.alerts_unread(1) == 1
    assert db.alerts_list(1)[0]["message"] == "시그널 관망 → 매수"

    db.alerts_mark_read(1)
    assert db.alerts_unread(1) == 0
    assert db.alerts_list(1)[0]["read"] is True


def test_alerts_scoped_by_uid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.alert_add(1, "A", "가", "m1")
    db.alert_add(2, "B", "나", "m2")
    assert db.alerts_unread(1) == 1 and db.alerts_unread(2) == 1
    assert [a["ticker"] for a in db.alerts_list(1)] == ["A"]
