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

# KB 원문(raw_text)은 저장만 되고 코드 어디서도 다시 안 읽힌다(다이제스트·목록·UI 모두 summary만 사용).
# 감사/재요약 대비로 앞부분만 남기고 절단해 app.db 비대를 막는다. 0이면 원문 미보관.
KB_RAW_TEXT_KEEP = 2000
# 자동 수집 뉴스 소스(큐레이션 업로드/리포트/인사이트는 prune 대상에서 제외 — 수동 신뢰 콘텐츠라 보존).
KB_NEWS_SOURCES = ("naver_news", "youtube")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE, pwhash TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, uid INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS profile(uid INTEGER PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS favorites(uid INTEGER, kind TEXT, key TEXT, label TEXT, ts INTEGER,
    PRIMARY KEY(uid, kind, key));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS user_bot(uid INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
    trading_style TEXT NOT NULL DEFAULT 'balanced', seed_cash REAL NOT NULL DEFAULT 10000000,
    seed_cash_us REAL NOT NULL DEFAULT 10000, updated INTEGER);
CREATE TABLE IF NOT EXISTS bot_positions(uid INTEGER, ticker TEXT, market TEXT NOT NULL DEFAULT 'kr', name TEXT, qty INTEGER,
    avg_price REAL, peak_price REAL, entry_date TEXT, last_price REAL, last_pnl_pct REAL, updated INTEGER,
    PRIMARY KEY(uid, ticker));
CREATE TABLE IF NOT EXISTS bot_trades(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, ticker TEXT,
    market TEXT NOT NULL DEFAULT 'kr', name TEXT,
    side TEXT, qty INTEGER, price REAL, reason TEXT, order_no TEXT, ts INTEGER, score REAL, note TEXT);
CREATE TABLE IF NOT EXISTS kb_entries(id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, title TEXT,
    summary TEXT, url TEXT UNIQUE, source TEXT, published TEXT, fetched INTEGER,
    doc_class TEXT, raw_text TEXT, status TEXT NOT NULL DEFAULT 'confirmed');
CREATE TABLE IF NOT EXISTS kb_digest(ticker TEXT PRIMARY KEY, name TEXT, sentiment REAL, summary TEXT,
    points TEXT, n_sources INTEGER, updated INTEGER, newest_ts INTEGER,
    event_flag INTEGER NOT NULL DEFAULT 0, event_note TEXT);
CREATE TABLE IF NOT EXISTS bot_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT,
    action TEXT, score REAL, rationale TEXT, context TEXT, decided_price REAL, ts INTEGER,
    outcome_pct REAL, outcome_ts INTEGER);
CREATE TABLE IF NOT EXISTS bot_reservations(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, ticker TEXT, name TEXT,
    side TEXT, target_price REAL, max_chase_pct REAL, reason TEXT, status TEXT, created INTEGER, resolved INTEGER,
    market TEXT NOT NULL DEFAULT 'kr');
CREATE TABLE IF NOT EXISTS holdings(uid INTEGER, ticker TEXT, qty REAL, avg_price REAL, ts INTEGER,
    PRIMARY KEY(uid, ticker));
CREATE TABLE IF NOT EXISTS alert_state(uid INTEGER, ticker TEXT, last_kind TEXT, updated INTEGER,
    PRIMARY KEY(uid, ticker));
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, ticker TEXT,
    name TEXT, message TEXT, ts INTEGER, read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS shortform(id TEXT PRIMARY KEY, ticker TEXT, name TEXT, kind TEXT, score REAL,
    title TEXT, script TEXT, caption TEXT, hashtags TEXT, card_svg TEXT, scenes TEXT,
    status TEXT NOT NULL DEFAULT 'draft', note TEXT, created INTEGER, reviewed INTEGER);
CREATE TABLE IF NOT EXISTS bot_equity(uid INTEGER, market TEXT NOT NULL DEFAULT 'kr', date TEXT,
    total_eval REAL, cash REAL, invested REAL, PRIMARY KEY(uid, market, date));
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
    # 레거시 단일계좌 봇 스키마(uid 없음) → 유저별로 재작성. 기존 봇 데이터는 폐기(paper/demo라 무방).
    pcols = {r[1] for r in c.execute("PRAGMA table_info(bot_positions)").fetchall()}
    if pcols and "uid" not in pcols:
        for t in ("bot_positions", "bot_trades", "bot_reservations", "bot_config"):
            c.execute(f"DROP TABLE IF EXISTS {t}")
        c.execute("DELETE FROM kv WHERE k='paper_account' OR k LIKE 'paper_account:%' OR k LIKE 'bot_day_equity%'")
        c.executescript(_SCHEMA)  # uid 스키마로 재생성
        pcols = {r[1] for r in c.execute("PRAGMA table_info(bot_positions)").fetchall()}
    if "market" not in pcols:  # 해외(US) 페이퍼 봇 — 시장 구분 컬럼(기존 행은 kr)
        c.execute("ALTER TABLE bot_positions ADD COLUMN market TEXT NOT NULL DEFAULT 'kr'")
    if "market" not in {r[1] for r in c.execute("PRAGMA table_info(bot_trades)").fetchall()}:
        c.execute("ALTER TABLE bot_trades ADD COLUMN market TEXT NOT NULL DEFAULT 'kr'")
    if "market" not in {r[1] for r in c.execute("PRAGMA table_info(bot_reservations)").fetchall()}:
        c.execute("ALTER TABLE bot_reservations ADD COLUMN market TEXT NOT NULL DEFAULT 'kr'")
    if "seed_cash_us" not in {r[1] for r in c.execute("PRAGMA table_info(user_bot)").fetchall()}:
        c.execute("ALTER TABLE user_bot ADD COLUMN seed_cash_us REAL NOT NULL DEFAULT 10000")
    dcols = {r[1] for r in c.execute("PRAGMA table_info(kb_digest)").fetchall()}
    if "newest_ts" not in dcols:  # 최신 원자료 발행 시각(신선도 판정용)
        c.execute("ALTER TABLE kb_digest ADD COLUMN newest_ts INTEGER")
    if "event_flag" not in dcols:  # 악재 이벤트 감지 여부(매수 후보 veto용)
        c.execute("ALTER TABLE kb_digest ADD COLUMN event_flag INTEGER NOT NULL DEFAULT 0")
    if "event_note" not in dcols:
        c.execute("ALTER TABLE kb_digest ADD COLUMN event_note TEXT")
    ecols = {r[1] for r in c.execute("PRAGMA table_info(kb_entries)").fetchall()}
    if "doc_class" not in ecols:  # 문서 유형(뉴스/리포트/공시/실적/이벤트/시황)
        c.execute("ALTER TABLE kb_entries ADD COLUMN doc_class TEXT")
    if "raw_text" not in ecols:  # 리포트·수동 입력 원문(뉴스는 NULL)
        c.execute("ALTER TABLE kb_entries ADD COLUMN raw_text TEXT")
    if "status" not in ecols:  # confirmed(다이제스트 반영) / pending(검토 보류, 반영 안 함)
        c.execute("ALTER TABLE kb_entries ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'")
    if "scenes" not in {r[1] for r in c.execute("PRAGMA table_info(shortform)").fetchall()}:
        c.execute("ALTER TABLE shortform ADD COLUMN scenes TEXT")  # 장면 시퀀스(인트로+근거별 프레임) JSON
    c.commit()
    # 일회성: 기존 raw_text 절단 + VACUUM(파일 회수). conn()이 매번 _migrate를 돌므로 kv 플래그로 1회만.
    # kv_get()은 conn()을 다시 열어 재귀되므로 여기선 c로 직접 조회한다.
    if KB_RAW_TEXT_KEEP >= 0 and c.execute(
            "SELECT 1 FROM kv WHERE k='kb_rawtext_trunc_v1'").fetchone() is None:
        c.execute("UPDATE kb_entries SET raw_text=substr(raw_text,1,?) "
                  "WHERE raw_text IS NOT NULL AND length(raw_text)>?",
                  (KB_RAW_TEXT_KEEP, KB_RAW_TEXT_KEEP))
        c.execute("INSERT OR REPLACE INTO kv(k,v,ts) VALUES('kb_rawtext_trunc_v1','1',?)",
                  (int(time.time()),))
        c.commit()
        try:
            c.execute("VACUUM")  # 절단으로 빈 페이지 → 파일 크기 실제 회수(트랜잭션 밖에서만 가능)
        except sqlite3.OperationalError:
            pass  # 다른 연결이 열려 있으면 스킵(다음 기회에 회수)


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


def fav_tickers_all() -> set[str]:
    """전 유저 관심종목 티커(중복 제거) — KB 갱신 대상 집계용(공용, bot_position_tickers_all와 동일 패턴)."""
    c = conn()
    rows = c.execute("SELECT DISTINCT key FROM favorites WHERE kind='ticker'").fetchall()
    c.close()
    return {r[0] for r in rows}


# ---------- alerts (#16 관심종목 시그널 변동 알림) ----------
def alert_state_all(uid: int) -> dict[str, str]:
    """uid의 종목별 마지막 관측 시그널 kind — 변동 감지 기준."""
    c = conn()
    rows = c.execute("SELECT ticker,last_kind FROM alert_state WHERE uid=?", (uid,)).fetchall()
    c.close()
    return {t: k for t, k in rows}


def alert_state_set(uid: int, ticker: str, kind: str) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO alert_state(uid,ticker,last_kind,updated) VALUES(?,?,?,?)",
              (uid, ticker, kind, int(time.time())))
    c.commit()
    c.close()


def alert_add(uid: int, ticker: str, name: str, message: str) -> None:
    c = conn()
    c.execute("INSERT INTO alerts(uid,ticker,name,message,ts,read) VALUES(?,?,?,?,?,0)",
              (uid, ticker, name, message, int(time.time())))
    c.commit()
    c.close()


def alerts_list(uid: int, limit: int = 30) -> list[dict]:
    c = conn()
    rows = c.execute("SELECT id,ticker,name,message,ts,read FROM alerts WHERE uid=? "
                     "ORDER BY id DESC LIMIT ?", (uid, limit)).fetchall()
    c.close()
    return [{"id": i, "ticker": t, "name": n, "message": m, "ts": ts, "read": bool(r)}
            for i, t, n, m, ts, r in rows]


def alerts_unread(uid: int) -> int:
    c = conn()
    n = c.execute("SELECT COUNT(*) FROM alerts WHERE uid=? AND read=0", (uid,)).fetchone()[0]
    c.close()
    return n


def alerts_mark_read(uid: int) -> None:
    c = conn()
    c.execute("UPDATE alerts SET read=1 WHERE uid=? AND read=0", (uid,))
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


# ---------- user_bot (유저별 봇 설정 — enabled/성향/시드) ----------
def user_bot_get(uid: int) -> dict:
    c = conn()
    c.execute("INSERT OR IGNORE INTO user_bot(uid,enabled,trading_style,seed_cash,seed_cash_us,updated) "
              "VALUES(?,0,'balanced',10000000,10000,?)", (uid, int(time.time())))
    c.commit()
    row = c.execute("SELECT enabled,trading_style,seed_cash,updated,seed_cash_us FROM user_bot WHERE uid=?",
                    (uid,)).fetchone()
    c.close()
    return {"enabled": bool(row[0]), "trading_style": row[1], "seed_cash": row[2], "updated": row[3],
            "seed_cash_us": row[4]}


def user_bot_set_enabled(uid: int, enabled: bool) -> None:
    user_bot_get(uid)
    c = conn()
    c.execute("UPDATE user_bot SET enabled=?, updated=? WHERE uid=?", (int(enabled), int(time.time()), uid))
    c.commit()
    c.close()


def user_bot_set_style(uid: int, style: str) -> None:
    user_bot_get(uid)
    c = conn()
    c.execute("UPDATE user_bot SET trading_style=?, updated=? WHERE uid=?", (style, int(time.time()), uid))
    c.commit()
    c.close()


def user_bot_set_seed(uid: int, seed_cash: float, market: str = "kr") -> None:
    user_bot_get(uid)
    col = "seed_cash_us" if market == "us" else "seed_cash"
    c = conn()
    c.execute(f"UPDATE user_bot SET {col}=?, updated=? WHERE uid=?", (seed_cash, int(time.time()), uid))
    c.commit()
    c.close()


def user_bots_enabled() -> list[int]:
    """봇이 켜진 유저 uid 목록 — 백그라운드 루프가 순회 대상."""
    c = conn()
    rows = c.execute("SELECT uid FROM user_bot WHERE enabled=1").fetchall()
    c.close()
    return [r[0] for r in rows]


# ---------- bot_positions (유저별·시장별) ----------
def bot_positions_all(uid: int, market: str = "kr") -> list[dict]:
    c = conn()
    rows = c.execute("SELECT ticker,name,qty,avg_price,peak_price,entry_date,last_price,last_pnl_pct "
                     "FROM bot_positions WHERE uid=? AND market=?", (uid, market)).fetchall()
    c.close()
    return [{"ticker": t, "name": n, "qty": q, "avg_price": ap, "peak_price": pk, "entry_date": ed,
             "last_price": lp, "last_pnl_pct": lr}
            for t, n, q, ap, pk, ed, lp, lr in rows]


def bot_position_get(uid: int, ticker: str) -> dict | None:
    c = conn()
    row = c.execute("SELECT ticker,name,qty,avg_price,peak_price,entry_date,last_price,last_pnl_pct "
                     "FROM bot_positions WHERE uid=? AND ticker=?", (uid, ticker)).fetchone()
    c.close()
    if not row:
        return None
    t, n, q, ap, pk, ed, lp, lr = row
    return {"ticker": t, "name": n, "qty": q, "avg_price": ap, "peak_price": pk, "entry_date": ed,
            "last_price": lp, "last_pnl_pct": lr}


def bot_position_upsert(uid: int, ticker: str, name: str, qty: int, avg_price: float, peak_price: float,
                         entry_date: str, last_price: float | None = None,
                         last_pnl_pct: float | None = None, market: str = "kr") -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO bot_positions"
              "(uid,ticker,market,name,qty,avg_price,peak_price,entry_date,last_price,last_pnl_pct,updated) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (uid, ticker, market, name, qty, avg_price, peak_price, entry_date, last_price, last_pnl_pct,
               int(time.time())))
    c.commit()
    c.close()


def bot_position_delete(uid: int, ticker: str) -> None:
    c = conn()
    c.execute("DELETE FROM bot_positions WHERE uid=? AND ticker=?", (uid, ticker))
    c.commit()
    c.close()


def bot_position_tickers_all() -> set[str]:
    """전 유저 보유 종목 티커(중복 제거) — KB 갱신 대상 집계용(공용)."""
    c = conn()
    rows = c.execute("SELECT DISTINCT ticker FROM bot_positions").fetchall()
    c.close()
    return {r[0] for r in rows}


def bot_reset(uid: int) -> None:
    """유저 봇 상태 초기화(설정 유지) — 국내·해외 포지션·거래내역·예약·일일기준선 + 페이퍼 현금(시드 리셋)."""
    c = conn()
    c.execute("DELETE FROM bot_positions WHERE uid=?", (uid,))
    c.execute("DELETE FROM bot_trades WHERE uid=?", (uid,))
    c.execute("DELETE FROM bot_reservations WHERE uid=?", (uid,))
    c.execute("DELETE FROM kv WHERE k=? OR k=? OR k LIKE ?",
              (f"paper_account:{uid}", f"paper_account:{uid}:us", f"bot_day_equity:{uid}%"))
    c.commit()
    c.close()


# ---------- bot_trades (유저별·시장별) ----------
def bot_trade_log(uid: int, ticker: str, name: str, side: str, qty: int, price: float, reason: str,
                   order_no: str | None, score: float | None = None, note: str | None = None,
                   market: str = "kr") -> None:
    """score=매매 시점 시그널 종합점수, note=타이밍·수량 산정 근거(사람이 읽는 한 줄)."""
    c = conn()
    c.execute("INSERT INTO bot_trades(uid,ticker,market,name,side,qty,price,reason,order_no,ts,score,note) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (uid, ticker, market, name, side, qty, price, reason, order_no, int(time.time()), score, note))
    c.commit()
    c.close()


def bot_trades_recent(uid: int, limit: int = 20, market: str = "kr") -> list[dict]:
    c = conn()
    rows = c.execute("SELECT ticker,name,side,qty,price,reason,order_no,ts,score,note FROM bot_trades "
                      "WHERE uid=? AND market=? ORDER BY id DESC LIMIT ?", (uid, market, limit)).fetchall()
    c.close()
    return [{"ticker": t, "name": n, "side": s, "qty": q, "price": p, "reason": r, "order_no": o,
             "ts": ts, "score": sc, "note": nt}
            for t, n, s, q, p, r, o, ts, sc, nt in rows]


# ---------- KB (뉴스·영상 가공 지식베이스) ----------
def kb_entry_add_many(ticker: str, items: list[dict]) -> int:
    """원자료 엔트리 저장(url UNIQUE + 배치 내 제목 중복 제거). 저장 건수 반환."""
    c = conn()
    added = 0
    seen_titles: set[str] = set()
    for it in items:
        if not it.get("url"):
            continue
        title = (it.get("title", "") or "").strip()
        if title and title in seen_titles:  # 같은 기사 다른 URL(재발행·연합송고) 중복 제거
            continue
        seen_titles.add(title)
        cur = c.execute("INSERT OR IGNORE INTO kb_entries(ticker,title,summary,url,source,published,fetched,doc_class) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (ticker, title, it.get("summary", ""), it["url"],
                         it.get("source", ""), it.get("published", ""), int(time.time()), it.get("doc_class")))
        added += cur.rowcount
    c.commit()
    c.close()
    return added


def kb_prune(news_per_ticker: int = 30, news_ttl_days: int = 90, pending_ttl_days: int = 14) -> dict:
    """KB 저장 정리(무한 누적 방지). 자동 뉴스만 대상 — 큐레이션 업로드/리포트/인사이트는 보존.
    - 뉴스: 종목당 최신 news_per_ticker건 초과 삭제. 단 다이제스트 하한(12건)은 보장하고,
      12건 초과분 중 news_ttl_days 지난 것도 삭제(오래된 뉴스는 시그널에 무의미).
    - pending 문서: pending_ttl_days 지나도 confirmed 안 되면 삭제(다이제스트 미반영·원문만 점유).
    반환: {news_deleted, pending_deleted}."""
    c = conn()
    now = int(time.time())
    placeholders = ",".join("?" * len(KB_NEWS_SOURCES))
    news_del = c.execute(
        f"DELETE FROM kb_entries WHERE source IN ({placeholders}) AND id IN ("
        f"  SELECT id FROM (SELECT id, fetched, ROW_NUMBER() OVER "
        f"    (PARTITION BY ticker ORDER BY id DESC) rn FROM kb_entries "
        f"    WHERE source IN ({placeholders})) "
        f"  WHERE rn > ? OR (rn > 12 AND fetched < ?))",
        (*KB_NEWS_SOURCES, *KB_NEWS_SOURCES, news_per_ticker, now - news_ttl_days * 86400),
    ).rowcount
    pend_del = c.execute(
        "DELETE FROM kb_entries WHERE status='pending' AND fetched < ?",
        (now - pending_ttl_days * 86400,),
    ).rowcount
    c.commit()
    c.close()
    return {"news_deleted": news_del, "pending_deleted": pend_del}


def kb_document_add(ticker: str, title: str, summary: str, url: str, source: str,
                    published: str, doc_class: str, raw_text: str | None = None,
                    status: str = "confirmed") -> int:
    """단일 문서 추가(리포트·수동 입력 등). url 없으면 유사고유키 생성. status=pending이면
    다이제스트(시그널)에 반영되지 않는다. row id 반환(-1=중복)."""
    c = conn()
    key = url or f"manual:{ticker}:{title}:{int(time.time())}"
    if raw_text and KB_RAW_TEXT_KEEP >= 0:  # 원문은 안 읽히므로 절단 보관(감사용 앞부분만)
        raw_text = raw_text[:KB_RAW_TEXT_KEEP] or None
    # 같은 url 재적재는 최신 내용·상태로 갱신(멱등 — 재크롤 시 freshness 반영, pending→confirmed 승격 포함)
    c.execute("INSERT INTO kb_entries(ticker,title,summary,url,source,published,fetched,doc_class,raw_text,status) "
              "VALUES(?,?,?,?,?,?,?,?,?,?) "
              "ON CONFLICT(url) DO UPDATE SET title=excluded.title, summary=excluded.summary, "
              "source=excluded.source, published=excluded.published, fetched=excluded.fetched, "
              "doc_class=excluded.doc_class, raw_text=excluded.raw_text, status=excluded.status",
              (ticker, title, summary, key, source, published, int(time.time()), doc_class, raw_text, status))
    c.commit()
    row = c.execute("SELECT id FROM kb_entries WHERE url=?", (key,)).fetchone()
    c.close()
    return row[0] if row else -1


def kb_document_urls(source: str | None = None) -> set[str]:
    """이미 적재된 문서 URL 집합 — 증분 수집(재수집 스킵)용. source로 필터 가능."""
    c = conn()
    if source:
        rows = c.execute("SELECT url FROM kb_entries WHERE source=? AND url IS NOT NULL", (source,)).fetchall()
    else:
        rows = c.execute("SELECT url FROM kb_entries WHERE url IS NOT NULL").fetchall()
    c.close()
    return {r[0] for r in rows}


def kb_documents(ticker: str | None = None, doc_class: str | None = None, limit: int = 100) -> list[dict]:
    """문서 대시보드용 — 전체(또는 필터) 문서 목록(최신순)."""
    c = conn()
    q = "SELECT id,ticker,title,summary,url,source,published,fetched,doc_class,status FROM kb_entries"
    where, args = [], []
    if ticker:
        where.append("ticker=?"); args.append(ticker)
    if doc_class:
        where.append("doc_class=?"); args.append(doc_class)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall()
    c.close()
    cols = ["id", "ticker", "title", "summary", "url", "source", "published", "fetched", "doc_class", "status"]
    return [dict(zip(cols, r)) for r in rows]


def kb_class_counts() -> dict[str, int]:
    """문서 유형별 건수(대시보드 필터 뱃지용)."""
    c = conn()
    rows = c.execute("SELECT COALESCE(doc_class,'미분류'), COUNT(*) FROM kb_entries GROUP BY doc_class").fetchall()
    c.close()
    return {k: n for k, n in rows}


def kb_entries_recent(ticker: str, limit: int = 12, confirmed_only: bool = False) -> list[dict]:
    c = conn()
    q = "SELECT title,summary,url,source,published FROM kb_entries WHERE ticker=? "
    if confirmed_only:  # 다이제스트(시그널 반영)는 confirmed만 — pending 문서는 제외해 오염 방지
        q += "AND status='confirmed' "
    rows = c.execute(q + "ORDER BY id DESC LIMIT ?", (ticker, limit)).fetchall()
    c.close()
    return [{"title": t, "summary": s, "url": u, "source": src, "published": p} for t, s, u, src, p in rows]


def kb_digest_set(ticker: str, name: str, sentiment: float, summary: str, points: list[str],
                  n_sources: int, newest_ts: int | None = None,
                  event_flag: bool = False, event_note: str | None = None) -> None:
    c = conn()
    c.execute("INSERT INTO kb_digest(ticker,name,sentiment,summary,points,n_sources,updated,newest_ts,event_flag,event_note) "
              "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
              "name=excluded.name, sentiment=excluded.sentiment, summary=excluded.summary, "
              "points=excluded.points, n_sources=excluded.n_sources, updated=excluded.updated, "
              "newest_ts=excluded.newest_ts, event_flag=excluded.event_flag, event_note=excluded.event_note",
              (ticker, name, sentiment, summary, json.dumps(points, ensure_ascii=False), n_sources,
               int(time.time()), newest_ts, 1 if event_flag else 0, event_note))
    c.commit()
    c.close()


_KB_COLS = "ticker,name,sentiment,summary,points,n_sources,updated,newest_ts,event_flag,event_note"


def _kb_row(row) -> dict:
    t, n, s, sm, p, ns, up, nts, ef, en = row
    return {"ticker": t, "name": n, "sentiment": s, "summary": sm,
            "points": json.loads(p or "[]"), "n_sources": ns, "updated": up,
            "newest_ts": nts, "event_flag": bool(ef), "event_note": en}


def kb_digest_get(ticker: str) -> dict | None:
    c = conn()
    row = c.execute(f"SELECT {_KB_COLS} FROM kb_digest WHERE ticker=?", (ticker,)).fetchone()
    c.close()
    return _kb_row(row) if row else None


def kb_digests_all() -> dict[str, dict]:
    c = conn()
    rows = c.execute(f"SELECT {_KB_COLS} FROM kb_digest").fetchall()
    c.close()
    return {r[0]: _kb_row(r) for r in rows}


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


def bot_decision_scorecard() -> dict:
    """실현된 매수 판단 성적표 — 승률·평균/최고/최악 실현수익(3일). 미실현(outcome_pct NULL) 제외.
    시그널이 실제로 맞았는지의 공개 증거(③ track record)."""
    c = conn()
    n, wins, avg, best, worst = c.execute(
        "SELECT COUNT(*), SUM(CASE WHEN outcome_pct>0 THEN 1 ELSE 0 END), "
        "AVG(outcome_pct), MAX(outcome_pct), MIN(outcome_pct) "
        "FROM bot_decisions WHERE action='buy' AND outcome_pct IS NOT NULL").fetchone()
    total = c.execute("SELECT COUNT(*) FROM bot_decisions WHERE action='buy'").fetchone()[0] or 0
    c.close()
    n = n or 0
    return {"resolved": n, "pending": total - n,
            "win_rate": round(wins / n * 100, 1) if n else None,
            "avg_outcome_pct": round(avg, 2) if avg is not None else None,
            "best_pct": round(best, 2) if best is not None else None,
            "worst_pct": round(worst, 2) if worst is not None else None}


def bot_decision_set_outcome(decision_id: int, outcome_pct: float) -> None:
    c = conn()
    c.execute("UPDATE bot_decisions SET outcome_pct=?, outcome_ts=? WHERE id=?",
              (outcome_pct, int(time.time()), decision_id))
    c.commit()
    c.close()


# ---------- bot_reservations (마감 후 예약 주문 — 유저별) ----------
def bot_reservation_add(uid: int, ticker: str, name: str, side: str, target_price: float,
                        max_chase_pct: float, reason: str, market: str = "kr") -> None:
    c = conn()
    c.execute("INSERT INTO bot_reservations(uid,ticker,name,side,target_price,max_chase_pct,reason,status,created,market) "
              "VALUES(?,?,?,?,?,?,?, 'pending', ?, ?)",
              (uid, ticker, name, side, target_price, max_chase_pct, reason, int(time.time()), market))
    c.commit()
    c.close()


def bot_reservations_pending(uid: int, market: str = "kr") -> list[dict]:
    c = conn()
    rows = c.execute("SELECT id,ticker,name,side,target_price,max_chase_pct,reason,created FROM bot_reservations "
                     "WHERE uid=? AND market=? AND status='pending' ORDER BY id", (uid, market)).fetchall()
    c.close()
    return [{"id": i, "ticker": t, "name": n, "side": s, "target_price": tp, "max_chase_pct": mc,
             "reason": r, "created": cr} for i, t, n, s, tp, mc, r, cr in rows]


def bot_reservation_resolve(res_id: int, status: str) -> None:
    c = conn()
    c.execute("UPDATE bot_reservations SET status=?, resolved=? WHERE id=?", (status, int(time.time()), res_id))
    c.commit()
    c.close()


def bot_reservations_clear_pending(uid: int, market: str = "kr") -> None:
    """유저의 미실행 예약 정리(시장별 — 새 마감 분석 전 pending을 만료 처리)."""
    c = conn()
    c.execute("UPDATE bot_reservations SET status='expired', resolved=? WHERE uid=? AND market=? AND status='pending'",
              (int(time.time()), uid, market))
    c.commit()
    c.close()


# ---------- holdings (유저 실제 보유종목 — 리밸런싱 대상) ----------
def holdings_list(uid: int) -> list[dict]:
    c = conn()
    rows = c.execute("SELECT ticker,qty,avg_price FROM holdings WHERE uid=? ORDER BY ts DESC", (uid,)).fetchall()
    c.close()
    return [{"ticker": t, "qty": q, "avg_price": ap} for t, q, ap in rows]


def holdings_set(uid: int, ticker: str, qty: float, avg_price: float) -> None:
    c = conn()
    c.execute("INSERT OR REPLACE INTO holdings(uid,ticker,qty,avg_price,ts) VALUES(?,?,?,?,?)",
              (uid, ticker, qty, avg_price, int(time.time())))
    c.commit()
    c.close()


def holdings_remove(uid: int, ticker: str) -> None:
    c = conn()
    c.execute("DELETE FROM holdings WHERE uid=? AND ticker=?", (uid, ticker))
    c.commit()
    c.close()


# ---------- shortform (숏폼 콘텐츠 초안 + 검수 큐 — 관리자 전용) ----------
def _shortform_row(r) -> dict:
    (sid, ticker, name, kind, score, title, script, caption, hashtags, card_svg, scenes,
     status, note, created, reviewed) = r
    return {"id": sid, "ticker": ticker, "name": name, "kind": kind, "score": score,
            "title": title, "script": json.loads(script) if script else [],
            "caption": caption, "hashtags": json.loads(hashtags) if hashtags else [],
            "card_svg": card_svg, "scenes": json.loads(scenes) if scenes else [],
            "status": status, "note": note, "created": created, "reviewed": reviewed}


_SHORTFORM_COLS = ("id,ticker,name,kind,score,title,script,caption,hashtags,card_svg,scenes,"
                   "status,note,created,reviewed")


def shortform_add(item: dict) -> None:
    """숏폼 초안 저장(status='draft'). item: {id,ticker,name,kind,score,title,script[],caption,hashtags[],card_svg,scenes[]}."""
    c = conn()
    c.execute(f"INSERT OR REPLACE INTO shortform({_SHORTFORM_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (item["id"], item.get("ticker"), item.get("name"), item.get("kind"), item.get("score"),
               item.get("title"), json.dumps(item.get("script") or [], ensure_ascii=False),
               item.get("caption"), json.dumps(item.get("hashtags") or [], ensure_ascii=False),
               item.get("card_svg"), json.dumps(item.get("scenes") or [], ensure_ascii=False),
               item.get("status", "draft"), item.get("note"), int(time.time()), None))
    c.commit()
    c.close()


def shortform_list(status: str | None = None, limit: int = 100) -> list[dict]:
    """검수 큐 목록(최신순). status 지정 시 해당 상태만. card_svg·scenes는 목록에선 제외(가벼움)."""
    cols = _SHORTFORM_COLS.replace("card_svg", "'' as card_svg").replace("scenes", "'' as scenes")  # 목록은 SVG 생략(용량)
    c = conn()
    if status:
        rows = c.execute(f"SELECT {cols} FROM shortform WHERE status=? ORDER BY created DESC LIMIT ?",
                         (status, limit)).fetchall()
    else:
        rows = c.execute(f"SELECT {cols} FROM shortform ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [_shortform_row(r) for r in rows]


def shortform_get(sid: str) -> dict | None:
    c = conn()
    r = c.execute(f"SELECT {_SHORTFORM_COLS} FROM shortform WHERE id=?", (sid,)).fetchone()
    c.close()
    return _shortform_row(r) if r else None


def shortform_set_status(sid: str, status: str, note: str = "") -> None:
    """검수 결과 반영 — approved|rejected|published 등."""
    c = conn()
    c.execute("UPDATE shortform SET status=?, note=?, reviewed=? WHERE id=?",
              (status, note or None, int(time.time()), sid))
    c.commit()
    c.close()


def shortform_delete(sid: str) -> None:
    c = conn()
    c.execute("DELETE FROM shortform WHERE id=?", (sid,))
    c.commit()
    c.close()


def shortform_recent_tickers(within_sec: int) -> set[str]:
    """최근 within_sec 이내 생성된 숏폼의 종목 집합 — 중복 생성 방지용."""
    c = conn()
    rows = c.execute("SELECT DISTINCT ticker FROM shortform WHERE created >= ?",
                     (int(time.time()) - within_sec,)).fetchall()
    c.close()
    return {t for (t,) in rows}


# ---------- bot_equity (봇 일별 자산 스냅샷 — track record 자산곡선) ----------
def bot_equity_record(uid: int, market: str, date: str, total_eval: float,
                      cash: float, invested: float) -> None:
    """하루 1점(날짜별 upsert — 같은 날 여러 번 실행되면 마지막 값으로 갱신)."""
    c = conn()
    c.execute("INSERT OR REPLACE INTO bot_equity(uid,market,date,total_eval,cash,invested) "
              "VALUES(?,?,?,?,?,?)", (uid, market, date, total_eval, cash, invested))
    c.commit()
    c.close()


def bot_equity_curve(uid: int, market: str = "kr", limit: int = 365) -> list[dict]:
    """자산곡선(오래된→최신) [{date,total_eval,cash,invested}]."""
    c = conn()
    rows = c.execute("SELECT date,total_eval,cash,invested FROM bot_equity WHERE uid=? AND market=? "
                     "ORDER BY date DESC LIMIT ?", (uid, market, limit)).fetchall()
    c.close()
    return [{"date": d, "total_eval": te, "cash": ca, "invested": iv}
            for d, te, ca, iv in reversed(rows)]
