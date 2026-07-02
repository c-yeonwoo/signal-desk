"""SQLite 통합 저장소 (data/cache/app.db).

1단계 스캐폴딩 범위: 인증·온보딩·워치리스트·범용 캐시만.
시세/시그널 전용 테이블은 2단계 시그널 엔진 도입 시 이 파일에 추가한다(kv로 임시 대체 가능).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DB = Path("data/cache/app.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE, pwhash TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, uid INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS profile(uid INTEGER PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS favorites(uid INTEGER, kind TEXT, key TEXT, label TEXT, ts INTEGER,
    PRIMARY KEY(uid, kind, key));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS bot_config(id INTEGER PRIMARY KEY CHECK (id=1),
    enabled INTEGER NOT NULL DEFAULT 0, max_positions INTEGER NOT NULL DEFAULT 10,
    position_pct REAL NOT NULL DEFAULT 0.08, updated INTEGER);
CREATE TABLE IF NOT EXISTS bot_positions(ticker TEXT PRIMARY KEY, name TEXT, qty INTEGER,
    avg_price REAL, peak_price REAL, entry_date TEXT, updated INTEGER);
CREATE TABLE IF NOT EXISTS bot_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT,
    side TEXT, qty INTEGER, price REAL, reason TEXT, order_no TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS kb_entries(id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, title TEXT,
    summary TEXT, url TEXT UNIQUE, source TEXT, published TEXT, fetched INTEGER);
CREATE TABLE IF NOT EXISTS kb_digest(ticker TEXT PRIMARY KEY, name TEXT, sentiment REAL, summary TEXT,
    points TEXT, n_sources INTEGER, updated INTEGER);
CREATE TABLE IF NOT EXISTS bot_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT,
    action TEXT, score REAL, rationale TEXT, context TEXT, decided_price REAL, ts INTEGER,
    outcome_pct REAL, outcome_ts INTEGER);
CREATE TABLE IF NOT EXISTS bot_reservations(id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT,
    side TEXT, target_price REAL, max_chase_pct REAL, reason TEXT, status TEXT, created INTEGER, resolved INTEGER);
"""


def conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(_SCHEMA)
    _migrate(c)
    return c


def _migrate(c: sqlite3.Connection) -> None:
    """가벼운 ADD COLUMN 마이그레이션 — CREATE TABLE IF NOT EXISTS는 기존 테이블에 새 컬럼을
    안 붙여줘서, 이미 만들어진 DB에도 신규 컬럼을 채워준다."""
    tcols = {r[1] for r in c.execute("PRAGMA table_info(bot_trades)").fetchall()}
    if "score" not in tcols:
        c.execute("ALTER TABLE bot_trades ADD COLUMN score REAL")
    if "note" not in tcols:
        c.execute("ALTER TABLE bot_trades ADD COLUMN note TEXT")
    ccols = {r[1] for r in c.execute("PRAGMA table_info(bot_config)").fetchall()}
    if "min_buy_score" not in ccols:  # 이 점수 이상인 BUY만 매수(약한 BUY는 제외)
        c.execute("ALTER TABLE bot_config ADD COLUMN min_buy_score REAL NOT NULL DEFAULT 1.6")
    if "max_new_buys_per_run" not in ccols:  # 한 사이클에 신규 매수 최대 건수(한꺼번에 다 사지 않음)
        c.execute("ALTER TABLE bot_config ADD COLUMN max_new_buys_per_run INTEGER NOT NULL DEFAULT 2")
    if "trading_style" not in ccols:  # 안정형/균형형/공격형 — 성향별 봇 파라미터 프리셋
        c.execute("ALTER TABLE bot_config ADD COLUMN trading_style TEXT NOT NULL DEFAULT 'balanced'")
    c.commit()


# ---------- users / sessions ----------
def user_create(email: str, pwhash: str) -> int | None:
    c = conn()
    try:
        cur = c.execute("INSERT INTO users(email,pwhash,created) VALUES(?,?,?)",
                        (email.lower().strip(), pwhash, int(time.time())))
        c.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # 이미 가입된 이메일
    finally:
        c.close()


def user_by_email(email: str):
    c = conn()
    row = c.execute("SELECT id,email,pwhash FROM users WHERE email=?", (email.lower().strip(),)).fetchone()
    c.close()
    return {"id": row[0], "email": row[1], "pwhash": row[2]} if row else None


def session_create(token: str, uid: int) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO sessions(token,uid,ts) VALUES(?,?,?)", (token, uid, int(time.time())))
    c.commit()
    c.close()


def session_user(token: str):
    if not token:
        return None
    c = conn()
    row = c.execute("SELECT u.id,u.email FROM sessions s JOIN users u ON u.id=s.uid WHERE s.token=?",
                    (token,)).fetchone()
    c.close()
    return {"id": row[0], "email": row[1]} if row else None


def session_delete(token: str) -> None:
    c = conn()
    c.execute("DELETE FROM sessions WHERE token=?", (token,))
    c.commit()
    c.close()


# ---------- profile (uid → JSON, 온보딩 데이터) ----------
def profile_get(uid: int) -> dict:
    c = conn()
    row = c.execute("SELECT data FROM profile WHERE uid=?", (uid,)).fetchone()
    c.close()
    return json.loads(row[0]) if row and row[0] else {}


def profile_set(uid: int, data: dict) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO profile(uid,data) VALUES(?,?)",
              (uid, json.dumps(data, ensure_ascii=False)))
    c.commit()
    c.close()


# ---------- favorites (워치리스트 — kind='ticker') ----------
def fav_list(uid: int) -> list[dict]:
    c = conn()
    rows = c.execute("SELECT kind,key,label FROM favorites WHERE uid=? ORDER BY ts DESC", (uid,)).fetchall()
    c.close()
    return [{"kind": k, "key": key, "label": lb} for k, key, lb in rows]


def fav_add(uid: int, kind: str, key: str, label: str) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO favorites(uid,kind,key,label,ts) VALUES(?,?,?,?,?)",
              (uid, kind, key, label, int(time.time())))
    c.commit()
    c.close()


def fav_remove(uid: int, kind: str, key: str) -> None:
    c = conn()
    c.execute("DELETE FROM favorites WHERE uid=? AND kind=? AND key=?", (uid, kind, key))
    c.commit()
    c.close()


# ---------- kv (범용 JSON 캐시) ----------
def kv_get(k: str, max_age: int | None = None):
    """캐시 값(JSON 역직렬화). 없거나 max_age(초) 초과 시 None."""
    c = conn()
    row = c.execute("SELECT v, ts FROM kv WHERE k=?", (k,)).fetchone()
    c.close()
    if not row:
        return None
    if max_age is not None and (time.time() - row[1]) > max_age:
        return None
    return json.loads(row[0])


def kv_set(k: str, v) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO kv(k,v,ts) VALUES(?,?,?)",
              (k, json.dumps(v, ensure_ascii=False), int(time.time())))
    c.commit()
    c.close()


# ---------- bot_config (서비스 전체 공유하는 단일 데모 계좌 — uid 스코프 없음) ----------
def bot_config_get() -> dict:
    c = conn()
    c.execute("INSERT OR IGNORE INTO bot_config(id,enabled,max_positions,position_pct,updated) "
              "VALUES(1,0,10,0.08,?)", (int(time.time()),))
    c.commit()
    row = c.execute("SELECT enabled,max_positions,position_pct,updated,min_buy_score,max_new_buys_per_run,trading_style "
                    "FROM bot_config WHERE id=1").fetchone()
    c.close()
    return {"enabled": bool(row[0]), "max_positions": row[1], "position_pct": row[2], "updated": row[3],
            "min_buy_score": row[4], "max_new_buys_per_run": row[5], "trading_style": row[6]}


def bot_config_set_style(style: str, params: dict) -> None:
    """트레이딩 성향 저장 + 성향 프리셋 숫자 파라미터를 함께 반영."""
    c = conn()
    c.execute("INSERT OR IGNORE INTO bot_config(id,enabled,max_positions,position_pct,updated) "
              "VALUES(1,0,10,0.08,?)", (int(time.time()),))
    c.execute("UPDATE bot_config SET trading_style=?, max_positions=?, position_pct=?, min_buy_score=?, "
              "max_new_buys_per_run=?, updated=? WHERE id=1",
              (style, params["max_positions"], params["position_pct"], params["min_buy_score"],
               params["max_new_buys_per_run"], int(time.time())))
    c.commit()
    c.close()


def bot_config_set_enabled(enabled: bool) -> None:
    c = conn()
    c.execute("INSERT OR IGNORE INTO bot_config(id,enabled,max_positions,position_pct,updated) "
              "VALUES(1,0,10,0.08,?)", (int(time.time()),))
    c.execute("UPDATE bot_config SET enabled=?, updated=? WHERE id=1", (int(enabled), int(time.time())))
    c.commit()
    c.close()


# ---------- bot_positions ----------
def bot_positions_all() -> list[dict]:
    c = conn()
    rows = c.execute("SELECT ticker,name,qty,avg_price,peak_price,entry_date FROM bot_positions").fetchall()
    c.close()
    return [{"ticker": t, "name": n, "qty": q, "avg_price": ap, "peak_price": pk, "entry_date": ed}
            for t, n, q, ap, pk, ed in rows]


def bot_position_get(ticker: str) -> dict | None:
    c = conn()
    row = c.execute("SELECT ticker,name,qty,avg_price,peak_price,entry_date FROM bot_positions "
                     "WHERE ticker=?", (ticker,)).fetchone()
    c.close()
    if not row:
        return None
    t, n, q, ap, pk, ed = row
    return {"ticker": t, "name": n, "qty": q, "avg_price": ap, "peak_price": pk, "entry_date": ed}


def bot_position_upsert(ticker: str, name: str, qty: int, avg_price: float, peak_price: float,
                         entry_date: str) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO bot_positions(ticker,name,qty,avg_price,peak_price,entry_date,updated) "
              "VALUES(?,?,?,?,?,?,?)", (ticker, name, qty, avg_price, peak_price, entry_date, int(time.time())))
    c.commit()
    c.close()


def bot_position_delete(ticker: str) -> None:
    c = conn()
    c.execute("DELETE FROM bot_positions WHERE ticker=?", (ticker,))
    c.commit()
    c.close()


def bot_reset() -> None:
    """봇 포지션·거래내역 전부 삭제(설정은 유지). 과거 유령거래 등 정합성 깨진 상태 초기화용."""
    c = conn()
    c.execute("DELETE FROM bot_positions")
    c.execute("DELETE FROM bot_trades")
    c.commit()
    c.close()


# ---------- bot_trades ----------
def bot_trade_log(ticker: str, name: str, side: str, qty: int, price: float, reason: str,
                   order_no: str | None, score: float | None = None, note: str | None = None) -> None:
    """score=매매 시점 시그널 종합점수, note=타이밍·수량 산정 근거(사람이 읽는 한 줄)."""
    c = conn()
    c.execute("INSERT INTO bot_trades(ticker,name,side,qty,price,reason,order_no,ts,score,note) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)",
              (ticker, name, side, qty, price, reason, order_no, int(time.time()), score, note))
    c.commit()
    c.close()


def bot_trades_recent(limit: int = 20) -> list[dict]:
    c = conn()
    rows = c.execute("SELECT ticker,name,side,qty,price,reason,order_no,ts,score,note FROM bot_trades "
                      "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [{"ticker": t, "name": n, "side": s, "qty": q, "price": p, "reason": r, "order_no": o,
             "ts": ts, "score": sc, "note": nt}
            for t, n, s, q, p, r, o, ts, sc, nt in rows]


# ---------- KB (뉴스·영상 가공 지식베이스) ----------
def kb_entry_add_many(ticker: str, items: list[dict]) -> int:
    """원자료 엔트리 저장(url UNIQUE로 중복 무시). 저장 건수 반환."""
    c = conn()
    added = 0
    for it in items:
        if not it.get("url"):
            continue
        cur = c.execute("INSERT OR IGNORE INTO kb_entries(ticker,title,summary,url,source,published,fetched) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (ticker, it.get("title", ""), it.get("summary", ""), it["url"],
                         it.get("source", ""), it.get("published", ""), int(time.time())))
        added += cur.rowcount
    c.commit()
    c.close()
    return added


def kb_entries_recent(ticker: str, limit: int = 12) -> list[dict]:
    c = conn()
    rows = c.execute("SELECT title,summary,url,source,published FROM kb_entries WHERE ticker=? "
                     "ORDER BY id DESC LIMIT ?", (ticker, limit)).fetchall()
    c.close()
    return [{"title": t, "summary": s, "url": u, "source": src, "published": p} for t, s, u, src, p in rows]


def kb_digest_set(ticker: str, name: str, sentiment: float, summary: str, points: list[str], n_sources: int) -> None:
    c = conn()
    c.execute("INSERT INTO kb_digest(ticker,name,sentiment,summary,points,n_sources,updated) "
              "VALUES(?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
              "name=excluded.name, sentiment=excluded.sentiment, summary=excluded.summary, "
              "points=excluded.points, n_sources=excluded.n_sources, updated=excluded.updated",
              (ticker, name, sentiment, summary, json.dumps(points, ensure_ascii=False), n_sources, int(time.time())))
    c.commit()
    c.close()


def kb_digest_get(ticker: str) -> dict | None:
    c = conn()
    row = c.execute("SELECT ticker,name,sentiment,summary,points,n_sources,updated FROM kb_digest "
                    "WHERE ticker=?", (ticker,)).fetchone()
    c.close()
    if not row:
        return None
    t, n, s, sm, p, ns, up = row
    return {"ticker": t, "name": n, "sentiment": s, "summary": sm,
            "points": json.loads(p or "[]"), "n_sources": ns, "updated": up}


def kb_digests_all() -> dict[str, dict]:
    c = conn()
    rows = c.execute("SELECT ticker,name,sentiment,summary,points,n_sources,updated FROM kb_digest").fetchall()
    c.close()
    return {t: {"ticker": t, "name": n, "sentiment": s, "summary": sm,
                "points": json.loads(p or "[]"), "n_sources": ns, "updated": up}
            for t, n, s, sm, p, ns, up in rows}


# ---------- bot_decisions (의사결정 저널 — 학습용) ----------
def bot_decision_log(ticker: str, name: str, action: str, score: float | None,
                     rationale: str, context: dict, decided_price: float) -> int:
    c = conn()
    cur = c.execute("INSERT INTO bot_decisions(ticker,name,action,score,rationale,context,decided_price,ts) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (ticker, name, action, score, rationale, json.dumps(context, ensure_ascii=False),
                     decided_price, int(time.time())))
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


def bot_decisions_recent(limit: int = 40) -> list[dict]:
    c = conn()
    rows = c.execute("SELECT ticker,name,action,score,rationale,context,decided_price,ts,outcome_pct,outcome_ts "
                     "FROM bot_decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [{"ticker": t, "name": n, "action": a, "score": sc, "rationale": r,
             "context": json.loads(cx or "{}"), "decided_price": dp, "ts": ts,
             "outcome_pct": op, "outcome_ts": ot}
            for t, n, a, sc, r, cx, dp, ts, op, ot in rows]


def bot_decision_set_outcome(decision_id: int, outcome_pct: float) -> None:
    c = conn()
    c.execute("UPDATE bot_decisions SET outcome_pct=?, outcome_ts=? WHERE id=?",
              (outcome_pct, int(time.time()), decision_id))
    c.commit()
    c.close()


# ---------- bot_reservations (마감 후 예약 주문) ----------
def bot_reservation_add(ticker: str, name: str, side: str, target_price: float,
                        max_chase_pct: float, reason: str) -> None:
    c = conn()
    c.execute("INSERT INTO bot_reservations(ticker,name,side,target_price,max_chase_pct,reason,status,created) "
              "VALUES(?,?,?,?,?,?, 'pending', ?)",
              (ticker, name, side, target_price, max_chase_pct, reason, int(time.time())))
    c.commit()
    c.close()


def bot_reservations_pending() -> list[dict]:
    c = conn()
    rows = c.execute("SELECT id,ticker,name,side,target_price,max_chase_pct,reason,created FROM bot_reservations "
                     "WHERE status='pending' ORDER BY id").fetchall()
    c.close()
    return [{"id": i, "ticker": t, "name": n, "side": s, "target_price": tp, "max_chase_pct": mc,
             "reason": r, "created": cr} for i, t, n, s, tp, mc, r, cr in rows]


def bot_reservation_resolve(res_id: int, status: str) -> None:
    c = conn()
    c.execute("UPDATE bot_reservations SET status=?, resolved=? WHERE id=?", (status, int(time.time()), res_id))
    c.commit()
    c.close()


def bot_reservations_clear_pending() -> None:
    """미실행 예약 정리(새 마감 분석 전 pending을 만료 처리)."""
    c = conn()
    c.execute("UPDATE bot_reservations SET status='expired', resolved=? WHERE status='pending'", (int(time.time()),))
    c.commit()
    c.close()
