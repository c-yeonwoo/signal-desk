"""DB 연결 비용과 동시성 — 프로덕션에서 로그인이 실패하고 전체가 느렸던 원인.

프로덕션 로그(2026-08-10):

    sqlite3.OperationalError: database is locked
      File "signal_desk/db.py", line 205, in conn
        c.executescript(_SCHEMA)
      File "signal_desk/db.py", line 326, in user_by_email

`conn()` 이 **연결할 때마다** 스키마 DDL 전체(`executescript` = 암묵 커밋 + 쓰기 락)와
마이그레이션 36개 구문을 돌렸다. `conn()` 은 `db.py` 안에서만 **111곳**에서 불린다.
첫 화면이 21개 API를 동시에 쏘면 서로 락을 뺏어 로그인(`user_by_email`)까지 실패했다.

실측 비교(12스레드 × 30회 읽기+쓰기):

    옛 방식(매번 스키마)   2.20초 · 예외 165건
    새 방식(1회 + WAL)     0.13초 · 예외   0건
"""

from __future__ import annotations

import sqlite3
import threading

from signal_desk import db


def test_schema_runs_once_per_database_not_per_connection(tmp_path, monkeypatch):
    """**스키마는 DB당 한 번이다.** 연결마다 돌면 그게 곧 연결마다 쓰기 락이다."""
    monkeypatch.chdir(tmp_path)
    db._schema_ready.clear()
    # `sqlite3.Connection` 은 불변 타입이라 메서드를 못 감싼다 — 스키마와 **같은 블록**에서
    # 도는 `_migrate` 호출 수로 센다(둘은 항상 같이 돈다).
    calls = {"n": 0}
    real = db._migrate
    monkeypatch.setattr(db, "_migrate", lambda c: (calls.__setitem__("n", calls["n"] + 1), real(c))[1])
    for _ in range(20):
        db.conn().close()
    assert calls["n"] == 1, f"스키마·마이그레이션을 {calls['n']}회 실행했다 — 연결마다 쓰기 락을 잡는다"


def test_moving_the_database_reruns_the_schema(tmp_path, monkeypatch):
    """가드는 **경로별**이어야 한다 — 프로세스 단위 불리언이면 새 DB에 스키마가 안 생긴다.

    검사가 `monkeypatch.chdir(tmp_path)` 로 DB를 옮기므로, 여기서 틀리면 테스트 전체가
    빈 DB를 보게 된다(그리고 그건 프로덕션이 아니라 검사만 깨지는 조용한 실패다).
    """
    monkeypatch.chdir(tmp_path)
    db._schema_ready.clear()
    db.conn().close()
    assert db.kv_get("nope") is None                 # 스키마가 생겼다는 뜻

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    db.conn().close()
    db.kv_set("k", "v")                              # 새 위치에도 스키마가 생겨야 쓴다
    assert db.kv_get("k") == "v"


def test_wal_and_busy_timeout_are_enabled(tmp_path, monkeypatch):
    """WAL — 쓰기 한 건이 읽기를 막지 않는다. busy_timeout — 겹치면 **기다린다**.

    sqlite 기본 busy_timeout은 0이라 한 순간이라도 겹치면 즉시 예외다.
    """
    monkeypatch.chdir(tmp_path)
    db._schema_ready.clear()
    c = db.conn()
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
    c.close()


def test_concurrent_readers_and_writers_do_not_raise(tmp_path, monkeypatch):
    """**로그인이 실패한 이유가 이것이다.** 동시 접근에서 예외가 나면 안 된다."""
    monkeypatch.chdir(tmp_path)
    db._schema_ready.clear()
    db.conn().close()
    errs: list[str] = []

    def worker(n: int) -> None:
        for i in range(20):
            try:
                c = db.conn()
                c.execute("SELECT count(*) FROM kv").fetchone()
                c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (f"k{n}", str(i)))
                c.commit()
                c.close()
            except Exception as e:                    # noqa: BLE001
                errs.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, f"동시 접근에서 {len(errs)}건 실패: {errs[:2]}"


def test_login_path_survives_concurrent_load(tmp_path, monkeypatch):
    """로그인은 **다른 요청이 몰리는 중에도** 되어야 한다 — 실측에서 여기가 먼저 죽었다."""
    monkeypatch.chdir(tmp_path)
    db._schema_ready.clear()
    db.conn().close()
    db.user_create("a@b.c", "hash")
    errs: list[str] = []
    stop = threading.Event()

    def noise() -> None:
        while not stop.is_set():
            try:
                c = db.conn()
                c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('n','1')")
                c.commit()
                c.close()
            except Exception as e:                    # noqa: BLE001
                errs.append(str(e))

    ns = [threading.Thread(target=noise, daemon=True) for _ in range(6)]
    for t in ns:
        t.start()
    try:
        for _ in range(30):
            assert db.user_by_email("a@b.c") is not None, "부하 중 로그인 조회가 실패했다"
    finally:
        stop.set()
        for t in ns:
            t.join(timeout=2)
    assert not errs, f"쓰기 부하에서 {len(errs)}건 실패: {errs[:2]}"
