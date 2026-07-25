"""북극성 D7 — 시그널 탭 방문 기록과 코호트 집계.

docs/north-star-d7.md 정의 그대로: 가입 다음날부터 7일 내 시그널 탭 재방문. D0는 제외하고,
아직 7일이 안 지난 유저는 분모에서 뺀다(넣으면 최근 가입자가 많을 때 D7이 낮게 보인다).
"""

import datetime
import importlib

from fastapi.testclient import TestClient


def _fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    return db_module


def _signup(db_module, email: str, days_ago: int) -> int:
    uid = db_module.user_create(email, "hash")
    created = int((datetime.datetime.now(datetime.timezone.utc)
                   - datetime.timedelta(days=days_ago)).timestamp())
    c = db_module.conn()
    c.execute("UPDATE users SET created=? WHERE id=?", (created, uid))
    c.commit()
    c.close()
    return uid


def _d0(db_module, uid: int) -> datetime.date:
    c = db_module.conn()
    created = c.execute("SELECT created FROM users WHERE id=?", (uid,)).fetchone()[0]
    c.close()
    from zoneinfo import ZoneInfo
    return datetime.datetime.fromtimestamp(created, ZoneInfo("Asia/Seoul")).date()


def test_same_day_revisit_counts_once(tmp_path, monkeypatch):
    db = _fresh(tmp_path, monkeypatch)
    uid = _signup(db, "a@e.com", 30)
    db.signal_visit_mark(uid, "2026-07-20")
    db.signal_visit_mark(uid, "2026-07-20")
    assert db.d7_metrics(today="2026-07-25")["visit_days_total"] == 1


def test_d0_visit_alone_is_not_a_return(tmp_path, monkeypatch):
    """가입 당일 방문은 '돌아온 것'이 아니다 — 온보딩 직후 조회를 리텐션으로 세면 안 된다."""
    db = _fresh(tmp_path, monkeypatch)
    uid = _signup(db, "a@e.com", 30)
    db.signal_visit_mark(uid, _d0(db, uid).isoformat())
    out = db.d7_metrics(today="2026-07-25")
    assert out["denominator"] == 1 and out["numerator"] == 0 and out["d7_pct"] == 0.0


def test_visit_inside_and_outside_window(tmp_path, monkeypatch):
    db = _fresh(tmp_path, monkeypatch)
    inside = _signup(db, "in@e.com", 30)
    outside = _signup(db, "out@e.com", 30)
    db.signal_visit_mark(inside, (_d0(db, inside) + datetime.timedelta(days=7)).isoformat())
    db.signal_visit_mark(outside, (_d0(db, outside) + datetime.timedelta(days=8)).isoformat())
    out = db.d7_metrics(today="2026-07-25")
    assert out["denominator"] == 2 and out["numerator"] == 1 and out["d7_pct"] == 50.0


def test_immature_cohort_excluded_from_denominator(tmp_path, monkeypatch):
    """가입 3일차 유저는 아직 실패한 게 아니다. 분모에 넣으면 D7이 구조적으로 낮아진다."""
    db = _fresh(tmp_path, monkeypatch)
    _signup(db, "old@e.com", 30)
    _signup(db, "new@e.com", 3)
    out = db.d7_metrics()
    assert out["denominator"] == 1 and out["pending_users"] == 1


def test_no_matured_cohort_returns_none_not_zero(tmp_path, monkeypatch):
    """표본 0에서 0%를 내보내면 '아무도 안 돌아왔다'로 읽힌다 — 판정 불가여야 한다."""
    db = _fresh(tmp_path, monkeypatch)
    _signup(db, "new@e.com", 2)
    out = db.d7_metrics()
    assert out["d7_pct"] is None and out["denominator"] == 0


def test_cohorts_grouped_by_signup_week(tmp_path, monkeypatch):
    db = _fresh(tmp_path, monkeypatch)
    a = _signup(db, "a@e.com", 30)
    _signup(db, "b@e.com", 60)
    db.signal_visit_mark(a, (_d0(db, a) + datetime.timedelta(days=2)).isoformat())
    out = db.d7_metrics()
    assert len(out["cohorts"]) == 2
    hit = [c for c in out["cohorts"] if c["returned"] == 1]
    assert len(hit) == 1 and hit[0]["d7_pct"] == 100.0


def test_signals_endpoint_records_visit_only_when_logged_in(tmp_path, monkeypatch):
    db = _fresh(tmp_path, monkeypatch)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    client = TestClient(api_module.app)

    client.get("/api/signals")                     # 비로그인 — 기록 없음
    assert db.d7_metrics()["visit_days_total"] == 0

    client.post("/api/auth/signup", json={"email": "u@e.com", "pw": "abcdef12"})
    client.get("/api/signals")
    client.get("/api/signals")
    assert db.d7_metrics()["visit_days_total"] == 1  # 같은 날 두 번 → 1건


def test_d7_endpoint_is_admin_only(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    client = TestClient(api_module.app)
    assert client.get("/api/d7").status_code == 401
    client.post("/api/auth/signup", json={"email": "u@e.com", "pw": "abcdef12"})
    assert client.get("/api/d7").status_code == 403
