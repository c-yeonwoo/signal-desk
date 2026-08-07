"""SQLite 통합 저장소 (data/cache/app.db).

1단계 스캐폴딩 범위: 인증·온보딩·워치리스트·범용 캐시만.
시세/시그널 전용 테이블은 2단계 시그널 엔진 도입 시 이 파일에 추가한다(kv로 임시 대체 가능).
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import time
from pathlib import Path
from zoneinfo import ZoneInfo

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
-- 북극성 D7 계측: 로그인 세션의 시그널 탭 조회를 (유저·KST날짜) 1건으로 남긴다.
-- docs/north-star-d7.md의 정의를 그대로 구현한 유일한 소스. 방문 시각은 진단용이고 집계는 날짜만 쓴다.
CREATE TABLE IF NOT EXISTS signal_visits(uid INTEGER, visit_date TEXT, ts INTEGER,
    PRIMARY KEY(uid, visit_date));
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
-- horizon_days: 채점 지평(거래일). NULL 이면 **지평이 섞인 옛 채점**이라 리프트 계산에서 뺀다.
-- 2026-08-05 진단: 채점이 `closes[-1]`(오늘 종가)을 써서 보유 기간이 "채점 루프가 돌 때까지"로
-- 판단마다 달랐다(실측 3.0~6.1일). 지평이 섞이면 비교 가능한 base rate 를 만들 수 없다.
CREATE TABLE IF NOT EXISTS bot_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, name TEXT,
    action TEXT, score REAL, rationale TEXT, context TEXT, decided_price REAL, ts INTEGER,
    outcome_pct REAL, outcome_ts INTEGER, horizon_days INTEGER, entry_date TEXT, exit_date TEXT);
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
CREATE TABLE IF NOT EXISTS brain_proposals(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
    title TEXT, body_ko TEXT, rationale_ko TEXT, patch TEXT, baseline TEXT, evidence TEXT,
    method_key TEXT, confidence TEXT, status TEXT NOT NULL DEFAULT 'draft',
    note TEXT, created INTEGER, reviewed INTEGER);
-- 감사 가설 큐 — LLM이 "이 숫자가 틀렸다면 왜일까"를 적어두는 곳. 판정권은 없다.
-- 반증 방법(falsifier)이 없는 항목은 저장하지 않는다. 승격은 사람이 테스트로 옮길 때만.
CREATE TABLE IF NOT EXISTS audit_hypotheses(id TEXT PRIMARY KEY, target TEXT, title TEXT,
    claim TEXT, falsifier TEXT, check_hint TEXT, severity TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'pending', note TEXT, created INTEGER, reviewed INTEGER);
CREATE TABLE IF NOT EXISTS bot_equity(uid INTEGER, market TEXT NOT NULL DEFAULT 'kr', date TEXT,
    total_eval REAL, cash REAL, invested REAL, PRIMARY KEY(uid, market, date));
CREATE TABLE IF NOT EXISTS kb_embeddings(
    entry_id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL,
    updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kb_sources(
    source_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    source_family TEXT NOT NULL,
    trust_tier TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    allowed_scopes TEXT NOT NULL DEFAULT '["stock"]',
    default_doc_class TEXT,
    decision_event_mode TEXT NOT NULL DEFAULT 'none',
    config_ref TEXT,
    last_collected_at INTEGER,
    last_result TEXT,
    last_error TEXT,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    lifecycle TEXT NOT NULL DEFAULT 'active',
    pinned INTEGER NOT NULL DEFAULT 0,
    collect_runs INTEGER NOT NULL DEFAULT 0,
    quality_score REAL,
    quality_note TEXT,
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kb_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    scope_type TEXT NOT NULL DEFAULT 'stock',
    ticker TEXT,
    sector TEXT,
    event_type TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'unknown',
    severity TEXT NOT NULL DEFAULT 'info',
    confidence REAL NOT NULL DEFAULT 1.0,
    trust_tier TEXT NOT NULL DEFAULT 'official',
    status TEXT NOT NULL DEFAULT 'confirmed',
    decision_eligible INTEGER NOT NULL DEFAULT 0,
    decision_action TEXT NOT NULL DEFAULT 'none',
    detected_at INTEGER NOT NULL,
    effective_at INTEGER,
    expires_at INTEGER,
    resolved_at INTEGER,
    summary TEXT,
    rationale TEXT,
    extractor_model TEXT,
    policy_version TEXT NOT NULL DEFAULT 'p0',
    created INTEGER NOT NULL,
    updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kb_event_evidence(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    entry_id INTEGER,
    source_key TEXT,
    url TEXT,
    published TEXT,
    evidence_text TEXT,
    support_role TEXT NOT NULL DEFAULT 'primary',
    trust_score REAL,
    created INTEGER NOT NULL,
    FOREIGN KEY(event_id) REFERENCES kb_events(id));
CREATE INDEX IF NOT EXISTS idx_kb_events_ticker ON kb_events(ticker, status);
CREATE INDEX IF NOT EXISTS idx_kb_events_expires ON kb_events(expires_at);
CREATE TABLE IF NOT EXISTS llm_usage(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    model TEXT NOT NULL,
    kind TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
-- 하네스 판정 이력 — **append-only**. UPDATE·DELETE 경로를 만들지 않는다.
-- 왜: harness_last.json 1슬롯을 덮어쓰는 구조라 "판정이 마지막으로 돌린 결과"였다. 설정 변경에는
-- 이력이 있는데(signal_config_history) 가장 중요한 산출물인 판정에는 없었다. 무엇을 언제 어떤
-- 설정으로 쟀는지 재구성할 수 없으면 그 판정은 증거가 아니다. docs/prd-harness-preregistration.md F5.
-- preregistered_id 가 NULL 이면 탐색 실행이며 정본이 될 수 없다(is_locked 는 항상 0).
CREATE TABLE IF NOT EXISTS hypo_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    built_at TEXT NOT NULL,          -- ISO(KST) 생성 시각
    as_of TEXT,                      -- 그날 거래일
    source TEXT NOT NULL,            -- llm | rules | fallback
    model TEXT,
    sectors_json TEXT NOT NULL,      -- 지목한 업종 키(중복 제거) — 사후 채점 대상
    tickers_json TEXT NOT NULL,      -- 그 업종의 대표 종목(그날 기준)
    tree_json TEXT NOT NULL          -- 원문 트리(재생용)
);
CREATE INDEX IF NOT EXISTS idx_hypo_runs_built ON hypo_runs(built_at DESC);

CREATE TABLE IF NOT EXISTS harness_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL,
    preregistered_id TEXT,
    score_source TEXT NOT NULL,
    market TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    harness_json TEXT NOT NULL,
    percentile REAL,
    threshold_pct REAL,
    n_registered INTEGER,
    periods INTEGER,
    empty_periods INTEGER,
    effective_periods INTEGER,
    pit_dates INTEGER,
    price_data_to TEXT,
    verdict TEXT,
    verdict_why TEXT,
    is_locked INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT,
    sharpe_json TEXT,
    note TEXT);
CREATE INDEX IF NOT EXISTS idx_harness_runs_ran_at ON harness_runs(ran_at);
CREATE INDEX IF NOT EXISTS idx_harness_runs_prereg ON harness_runs(preregistered_id, ran_at);
"""


def conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(_SCHEMA)
    _migrate(c)
    return c


_REFERENCE_BOT_UIDS = (900001, 900002, 900003)  # bot.REFERENCE_BOTS와 같은 값(순환 import 회피)


_PERSONAL_PAPER_DROPPED = "migrated:personal_paper_dropped"


def _drop_personal_paper_accounts(c: sqlite3.Connection) -> None:
    """개인 페이퍼 계좌 잔여 데이터 1회 정리 — 남은 페이퍼 장부는 레퍼런스 3봇뿐이다(2026-07-27).

    켜기·시드·초기화 경로가 사라졌으므로 개인 uid의 봇 행은 다시 늘지 않는다. 매 연결마다 도는
    영구 청소부로 두면 안 된다 — 그건 마이그레이션이 아니라 '개인 uid는 봇을 못 갖는다'는 규칙을
    DELETE로 강제하는 것이고, 나중에 다른 이유로 uid를 쓰면 조용히 지워버린다."""
    if c.execute("SELECT 1 FROM kv WHERE k=?", (_PERSONAL_PAPER_DROPPED,)).fetchone():
        return
    keep = ",".join(str(u) for u in _REFERENCE_BOT_UIDS)
    for t in ("user_bot", "bot_positions", "bot_trades", "bot_reservations", "bot_equity"):
        c.execute(f"DELETE FROM {t} WHERE uid NOT IN ({keep})")
    rows = c.execute("SELECT k FROM kv WHERE k LIKE 'paper_account:%' OR k LIKE 'bot_day_equity%'").fetchall()
    for (k,) in rows:
        parts = k.split(":")
        if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) not in _REFERENCE_BOT_UIDS:
            c.execute("DELETE FROM kv WHERE k=?", (k,))
    c.execute("INSERT OR REPLACE INTO kv(k,v,ts) VALUES(?,?,?)",
              (_PERSONAL_PAPER_DROPPED, "1", int(time.time())))
    c.commit()


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
    bdcols = {r[1] for r in c.execute("PRAGMA table_info(bot_decisions)").fetchall()}
    for col, ddl in (("horizon_days", "INTEGER"), ("entry_date", "TEXT"), ("exit_date", "TEXT")):
        if bdcols and col not in bdcols:
            c.execute(f"ALTER TABLE bot_decisions ADD COLUMN {col} {ddl}")
    _drop_personal_paper_accounts(c)
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
    hcols = {r[1] for r in c.execute("PRAGMA table_info(harness_runs)").fetchall()}
    if hcols and "sharpe_json" not in hcols:   # L3 DSR용 기간 Sharpe(시도 간 분산 실측)
        c.execute("ALTER TABLE harness_runs ADD COLUMN sharpe_json TEXT")
    # kb_embeddings·kb_events는 _SCHEMA CREATE IF NOT EXISTS로 충분
    scols = {r[1] for r in c.execute("PRAGMA table_info(kb_sources)").fetchall()}
    if scols:  # 기존 DB에 수습·퇴출 컬럼 보강
        if "lifecycle" not in scols:
            c.execute("ALTER TABLE kb_sources ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active'")
        if "pinned" not in scols:
            c.execute("ALTER TABLE kb_sources ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if "collect_runs" not in scols:
            c.execute("ALTER TABLE kb_sources ADD COLUMN collect_runs INTEGER NOT NULL DEFAULT 0")
        if "quality_score" not in scols:
            c.execute("ALTER TABLE kb_sources ADD COLUMN quality_score REAL")
        if "quality_note" not in scols:
            c.execute("ALTER TABLE kb_sources ADD COLUMN quality_note TEXT")
    _seed_kb_sources(c)
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


def signal_visit_mark(uid: int, visit_date: str) -> None:
    """시그널 탭 조회 1일 1건 기록(북극성 D7 분자). 같은 날 재방문은 무시."""
    c = conn()
    c.execute("INSERT OR IGNORE INTO signal_visits(uid,visit_date,ts) VALUES(?,?,?)",
              (uid, visit_date, int(time.time())))
    c.commit()
    c.close()


def d7_metrics(*, window: int = 7, today: str | None = None) -> dict:
    """북극성 D7 — 가입 후 D1~D`window` 중 시그널 탭을 하루라도 다시 연 유저 비율.

    분모는 **코호트 완성분**(가입 후 window+1일 이상 경과)만. 아직 기간이 남은 유저는
    `pending`으로 따로 센다 — 분모에 넣으면 최근 가입자가 많을 때 D7이 실제보다 낮게 보인다.
    docs/north-star-d7.md의 정의를 그대로 따른다(D0 제외, 페이퍼·인사이트 조회 비포함).
    """
    tz = ZoneInfo("Asia/Seoul")
    today_d = (datetime.date.fromisoformat(today) if today
               else datetime.datetime.now(tz).date())
    c = conn()
    users = c.execute("SELECT id, created FROM users").fetchall()
    visits = c.execute("SELECT uid, visit_date FROM signal_visits").fetchall()
    c.close()

    dates_by_uid: dict[int, set[str]] = {}
    for uid, vd in visits:
        dates_by_uid.setdefault(uid, set()).add(vd)

    matured = returned = pending = 0
    weeks: dict[str, dict] = {}
    for uid, created in users:
        if not created:
            continue
        d0 = datetime.datetime.fromtimestamp(created, tz).date()
        if (today_d - d0).days <= window:
            pending += 1
            continue
        matured += 1
        seen = dates_by_uid.get(uid, set())
        hit = any((d0 + datetime.timedelta(days=k)).isoformat() in seen
                  for k in range(1, window + 1))
        returned += 1 if hit else 0
        wk = (d0 - datetime.timedelta(days=d0.weekday())).isoformat()  # 코호트 = 가입 주(월요일)
        w = weeks.setdefault(wk, {"cohort_week": wk, "n": 0, "returned": 0})
        w["n"] += 1
        w["returned"] += 1 if hit else 0

    for w in weeks.values():
        w["d7_pct"] = round(w["returned"] / w["n"] * 100, 1) if w["n"] else None
    return {
        "window_days": window,
        "denominator": matured,
        "numerator": returned,
        "d7_pct": round(returned / matured * 100, 1) if matured else None,
        "pending_users": pending,          # 코호트 미완성(아직 판정 불가)
        "visit_days_total": len(visits),
        "cohorts": sorted(weeks.values(), key=lambda w: w["cohort_week"], reverse=True)[:8],
        "definition": "가입 다음날부터 7일 내 시그널 탭(GET /api/signals) 재방문",
    }


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
    """봇이 켜진 uid 목록. 개인 페이퍼 봇 제거 후에는 레퍼런스 봇만 여기 남는다."""
    c = conn()
    rows = c.execute("SELECT uid FROM user_bot WHERE enabled=1").fetchall()
    c.close()
    return [r[0] for r in rows]


def uids_with_ticker_favorites() -> list[int]:
    """관심종목을 하나라도 가진 유저 — 시그널 변동 알림 스캔 대상.

    이전에는 '봇을 켠 유저'만 스캔해서, 알림이라는 별개 기능이 페이퍼 봇 활성화에 딸려 있었다.
    기능의 대상 집합은 그 기능이 정의하는 것이지 옆 기능의 on/off가 정하는 게 아니다."""
    c = conn()
    rows = c.execute("SELECT DISTINCT uid FROM favorites WHERE kind='ticker'").fetchall()
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


def kb_embedding_upsert(entry_id: int, model: str, vec: bytes) -> None:
    c = conn()
    c.execute("INSERT INTO kb_embeddings(entry_id,model,dim,vec,updated) VALUES(?,?,?,?,?) "
              "ON CONFLICT(entry_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, "
              "vec=excluded.vec, updated=excluded.updated",
              (entry_id, model, len(vec) // 4, vec, int(time.time())))
    c.commit()
    c.close()


def kb_embeddings_for_model(model: str, entry_ids: list[int] | None = None) -> dict[int, bytes]:
    c = conn()
    if entry_ids is not None:
        if not entry_ids:
            c.close()
            return {}
        ph = ",".join("?" * len(entry_ids))
        rows = c.execute(f"SELECT entry_id, vec FROM kb_embeddings WHERE model=? AND entry_id IN ({ph})",
                         (model, *entry_ids)).fetchall()
    else:
        rows = c.execute("SELECT entry_id, vec FROM kb_embeddings WHERE model=?", (model,)).fetchall()
    c.close()
    return {int(eid): blob for eid, blob in rows}


def kb_entries_missing_embed(model: str, limit: int = 80) -> list[dict]:
    """현재 모델 임베딩이 없는 confirmed 엔트리(최신순)."""
    c = conn()
    rows = c.execute(
        "SELECT e.id, e.ticker, e.title, e.summary FROM kb_entries e "
        "LEFT JOIN kb_embeddings m ON m.entry_id=e.id AND m.model=? "
        "WHERE e.status='confirmed' AND m.entry_id IS NULL "
        "ORDER BY e.id DESC LIMIT ?",
        (model, limit),
    ).fetchall()
    c.close()
    return [{"id": r[0], "ticker": r[1], "title": r[2], "summary": r[3]} for r in rows]


def kb_prune_orphan_embeddings() -> int:
    """엔트리 삭제 후 남은 고아 임베딩 정리."""
    c = conn()
    n = c.execute("DELETE FROM kb_embeddings WHERE entry_id NOT IN (SELECT id FROM kb_entries)").rowcount
    c.commit()
    c.close()
    return n


def kb_prune(news_per_ticker: int = 30, news_ttl_days: int = 90, pending_ttl_days: int = 14,
             insight_keep: int = 60, insight_ttl_days: int = 180) -> dict:
    """KB 저장 정리(무한 누적 방지). 자동 수집만 대상 — 큐레이션 업로드/리포트는 보존.
    - 뉴스: 종목당 최신 news_per_ticker건 초과 삭제. 단 다이제스트 하한(12건)은 보장하고,
      12건 초과분 중 news_ttl_days 지난 것도 삭제(오래된 뉴스는 시그널에 무의미).
    - pending 문서: pending_ttl_days 지나도 confirmed 안 되면 삭제(다이제스트 미반영·원문만 점유).
    - 인사이트(시황·거시, source='insight': 미주은·오건영·유튜브·해외RSS): 거시 다이제스트는 최근
      12건만 쓰는데 원본(raw_text)이 무한 누적 → 최신 insight_keep건 보장, 12건 초과분 중 insight_ttl_days
      지난 것 삭제(오래된 시황 논평은 재사용 가치 낮음).
    반환: {news_deleted, pending_deleted, insight_deleted}."""
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
    insight_del = c.execute(
        "DELETE FROM kb_entries WHERE source='insight' AND id IN ("
        "  SELECT id FROM (SELECT id, fetched, ROW_NUMBER() OVER "
        "    (PARTITION BY ticker ORDER BY id DESC) rn FROM kb_entries WHERE source='insight') "
        "  WHERE rn > ? OR (rn > 12 AND fetched < ?))",
        (insight_keep, now - insight_ttl_days * 86400),
    ).rowcount
    c.commit()
    c.close()
    orphan = kb_prune_orphan_embeddings()
    return {"news_deleted": news_del, "pending_deleted": pend_del, "insight_deleted": insight_del,
            "embed_orphans_deleted": orphan}


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
    eid = row[0] if row else -1
    if eid > 0 and status == "confirmed":
        try:
            from signal_desk import kb_embed
            kb_embed.upsert_entry(eid, title, summary)
        except Exception:
            pass  # 임베딩 실패해도 문서 적재는 성공
    return eid


def kb_document_urls(source: str | None = None) -> set[str]:
    """이미 적재된 문서 URL 집합 — 증분 수집(재수집 스킵)용. source로 필터 가능."""
    c = conn()
    if source:
        rows = c.execute("SELECT url FROM kb_entries WHERE source=? AND url IS NOT NULL", (source,)).fetchall()
    else:
        rows = c.execute("SELECT url FROM kb_entries WHERE url IS NOT NULL").fetchall()
    c.close()
    return {r[0] for r in rows}


def kb_entry_urls_existing(urls: list[str]) -> set[str]:
    """주어진 URL 중 이미 kb_entries에 있는 것만 반환 — refresh 증분·Sonnet 재호출 방지."""
    clean = [u for u in urls if u]
    if not clean:
        return set()
    c = conn()
    found: set[str] = set()
    for i in range(0, len(clean), 80):
        chunk = clean[i:i + 80]
        ph = ",".join("?" * len(chunk))
        rows = c.execute(f"SELECT url FROM kb_entries WHERE url IN ({ph})", chunk).fetchall()
        found.update(r[0] for r in rows)
    c.close()
    return found


def kb_doc_counts(*, before_ts: float | None = None) -> dict[str, int]:
    """ticker -> confirmed 원문 문서 수. before_ts를 주면 그 시점 이전에 수집된 것만 센다.

    PIT 재구성용이지만 완전하지 않다 — `kb_prune`이 지운 문서는 과거에 존재했더라도 여기 없어서
    오래된 날짜일수록 과소집계된다(보존 한도 안의 최근 구간에서만 쓸 것)."""
    c = conn()
    q = "SELECT ticker, COUNT(*) FROM kb_entries WHERE status='confirmed'"
    args: list = []
    if before_ts is not None:
        q += " AND fetched < ?"
        args.append(int(before_ts))
    rows = c.execute(q + " GROUP BY ticker", args).fetchall()
    c.close()
    return {r[0]: int(r[1]) for r in rows}


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


# ---------- kb_sources (수집 소스 레지스트리 — P1 ingest gate) ----------
_KB_SOURCE_SEEDS = (
    # source_key, display_name, family, trust_tier, scopes_json, doc_class, decision_event_mode
    ("dart", "DART 공시", "dart", "official", '["stock"]', "공시", "rule_official"),
    ("naver_news", "네이버 증권 뉴스", "naver_news", "medium", '["stock"]', "뉴스", "none"),
    ("youtube", "유튜브(화이트리스트)", "youtube", "medium", '["market"]', "시황", "none"),
    ("rss", "해외 전문가 RSS", "rss", "high", '["market"]', "시황", "none"),
    ("fanding", "미주은(fanding)", "fanding", "high", '["stock","market"]', "시황", "none"),
    ("outstanding", "아웃스탠딩", "outstanding", "high", '["stock","market"]', "시황", "none"),
    ("manual", "관리자 수동 업로드", "manual", "high", '["stock","sector","market"]', "리포트", "none"),
)


def _seed_kb_sources(c: sqlite3.Connection) -> None:
    """패밀리 소스 멱등 시드 — 기존 행의 enabled/카운터·lifecycle은 덮지 않음.
    패밀리 루트는 pinned=1(자동 퇴출 제외)."""
    now = int(time.time())
    for key, name, fam, tier, scopes, dclass, mode in _KB_SOURCE_SEEDS:
        c.execute(
            "INSERT INTO kb_sources(source_key,display_name,source_family,trust_tier,enabled,"
            "allowed_scopes,default_doc_class,decision_event_mode,lifecycle,pinned,"
            "collect_runs,created,updated) "
            "VALUES(?,?,?,?,1,?,?,?,'active',1,0,?,?) "
            "ON CONFLICT(source_key) DO UPDATE SET "
            "display_name=excluded.display_name, source_family=excluded.source_family, "
            "trust_tier=excluded.trust_tier, allowed_scopes=excluded.allowed_scopes, "
            "default_doc_class=excluded.default_doc_class, "
            "decision_event_mode=excluded.decision_event_mode, "
            "pinned=max(kb_sources.pinned, 1), updated=excluded.updated",
            (key, name, fam, tier, scopes, dclass, mode, now, now),
        )


_SRC_COLS = (
    "source_key,display_name,source_family,trust_tier,enabled,allowed_scopes,default_doc_class,"
    "decision_event_mode,config_ref,last_collected_at,last_result,last_error,"
    "accepted_count,pending_count,rejected_count,lifecycle,pinned,collect_runs,"
    "quality_score,quality_note,created,updated"
)


def _src_row(r) -> dict:
    d = dict(zip(_SRC_COLS.split(","), r))
    d["enabled"] = bool(d.get("enabled"))
    d["pinned"] = bool(d.get("pinned"))
    d["lifecycle"] = d.get("lifecycle") or "active"
    d["collect_runs"] = int(d.get("collect_runs") or 0)
    try:
        d["allowed_scopes"] = json.loads(d.get("allowed_scopes") or "[]")
    except Exception:
        d["allowed_scopes"] = []
    return d


def kb_source_get(source_key: str) -> dict | None:
    c = conn()
    row = c.execute(f"SELECT {_SRC_COLS} FROM kb_sources WHERE source_key=?", (source_key,)).fetchone()
    c.close()
    return _src_row(row) if row else None


def kb_sources_list(*, lifecycle: str | None = None) -> list[dict]:
    c = conn()
    q = f"SELECT {_SRC_COLS} FROM kb_sources"
    args: list = []
    if lifecycle:
        q += " WHERE lifecycle=?"; args.append(lifecycle)
    q += (" ORDER BY CASE lifecycle WHEN 'eviction_candidate' THEN 0 WHEN 'probation' THEN 1 ELSE 2 END, "
          "CASE trust_tier WHEN 'official' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, "
          "source_key")
    rows = c.execute(q, args).fetchall()
    c.close()
    return [_src_row(r) for r in rows]


def kb_source_ensure(source_key: str, *, display_name: str | None = None,
                     parent_key: str | None = None) -> dict | None:
    """소스 조회·없으면 생성. parent_key면 패밀리 tier/정책을 상속(채널·피드 lazy upsert).
    자식 채널/피드는 lifecycle=probation(수습)으로 시작."""
    existing = kb_source_get(source_key)
    if existing:
        return existing
    now = int(time.time())
    parent = kb_source_get(parent_key) if parent_key else None
    if parent:
        fam, tier = parent["source_family"], parent["trust_tier"]
        scopes = json.dumps(parent.get("allowed_scopes") or ["market"], ensure_ascii=False)
        dclass = parent.get("default_doc_class")
        mode = parent.get("decision_event_mode") or "none"
        name = display_name or source_key
        life = "probation"  # 채널·피드 수습
        pinned = 0
    else:
        return None
    c = conn()
    c.execute(
        "INSERT OR IGNORE INTO kb_sources(source_key,display_name,source_family,trust_tier,enabled,"
        "allowed_scopes,default_doc_class,decision_event_mode,lifecycle,pinned,collect_runs,"
        "created,updated) VALUES(?,?,?,?,1,?,?,?,?,?,0,?,?)",
        (source_key, name, fam, tier, scopes, dclass, mode, life, pinned, now, now),
    )
    c.commit()
    c.close()
    return kb_source_get(source_key)


def kb_sources_touch(source_key: str, result: str, *, error: str | None = None,
                     accepted: int = 0, pending: int = 0, rejected: int = 0) -> None:
    """수집 결과 카운터·시각 갱신."""
    now = int(time.time())
    c = conn()
    c.execute(
        "UPDATE kb_sources SET last_collected_at=?, last_result=?, last_error=?,"
        "accepted_count=accepted_count+?, pending_count=pending_count+?, "
        "rejected_count=rejected_count+?, updated=? WHERE source_key=?",
        (now, result, (error or "")[:240] or None, accepted, pending, rejected, now, source_key),
    )
    c.commit()
    c.close()


def kb_sources_bump_run(source_key: str) -> None:
    """채널/피드 1회 수집 패스 카운트(+1)."""
    now = int(time.time())
    c = conn()
    c.execute(
        "UPDATE kb_sources SET collect_runs=collect_runs+1, updated=? WHERE source_key=?",
        (now, source_key),
    )
    c.commit()
    c.close()


def kb_source_set_quality(source_key: str, *, score: float | None, note: str,
                          lifecycle: str | None = None) -> dict | None:
    """품질 점수·메모·(선택) lifecycle 갱신. 자동 disable은 하지 않음."""
    now = int(time.time())
    c = conn()
    if lifecycle:
        c.execute(
            "UPDATE kb_sources SET quality_score=?, quality_note=?, lifecycle=?, updated=? "
            "WHERE source_key=?",
            (score, (note or "")[:240], lifecycle, now, source_key),
        )
    else:
        c.execute(
            "UPDATE kb_sources SET quality_score=?, quality_note=?, updated=? WHERE source_key=?",
            (score, (note or "")[:240], now, source_key),
        )
    c.commit()
    c.close()
    return kb_source_get(source_key)


def kb_source_lifecycle_action(source_key: str, action: str) -> dict:
    """관리자 수습/퇴출 조치. pin|unpin|keep|evict|reprobation.
    evict만 enabled=0. 자동 퇴출 없음."""
    src = kb_source_get(source_key)
    if not src:
        return {"ok": False, "reason": "소스 없음"}
    now = int(time.time())
    c = conn()
    if action == "pin":
        c.execute("UPDATE kb_sources SET pinned=1, lifecycle='active', updated=? WHERE source_key=?",
                  (now, source_key))
    elif action == "unpin":
        c.execute("UPDATE kb_sources SET pinned=0, updated=? WHERE source_key=?", (now, source_key))
    elif action == "keep":
        c.execute(
            "UPDATE kb_sources SET lifecycle='active', enabled=1, quality_note=?, updated=? "
            "WHERE source_key=?",
            ("관리자 유지", now, source_key),
        )
    elif action == "evict":
        c.execute(
            "UPDATE kb_sources SET enabled=0, lifecycle='eviction_candidate', quality_note=?, "
            "updated=? WHERE source_key=?",
            ("관리자 퇴출 확정", now, source_key),
        )
    elif action == "reprobation":
        c.execute(
            "UPDATE kb_sources SET lifecycle='probation', enabled=1, collect_runs=0, "
            "quality_score=NULL, quality_note=?, updated=? WHERE source_key=?",
            ("수습 재시작", now, source_key),
        )
    else:
        c.close()
        return {"ok": False, "reason": f"unknown action: {action}"}
    c.commit()
    c.close()
    return {"ok": True, "source": kb_source_get(source_key)}


# ---------- kb_events (구조화 이벤트 카드 — Decision 입력) ----------
_EVT_COLS = (
    "id,event_key,scope_type,ticker,sector,event_type,direction,severity,confidence,"
    "trust_tier,status,decision_eligible,decision_action,detected_at,effective_at,"
    "expires_at,resolved_at,summary,rationale,extractor_model,policy_version,created,updated"
)


def _evt_row(r) -> dict:
    keys = _EVT_COLS.split(",")
    d = dict(zip(keys, r))
    d["decision_eligible"] = bool(d.get("decision_eligible"))
    return d


def kb_event_exists(event_key: str) -> bool:
    """event_key 존재 여부 — lite poll에서 '신규' 판정용."""
    c = conn()
    row = c.execute("SELECT 1 FROM kb_events WHERE event_key=?", (event_key,)).fetchone()
    c.close()
    return row is not None


def kb_event_upsert(event: dict, evidence: dict | None = None) -> int:
    """event_key 기준 upsert. evidence가 있으면 primary evidence 1건 보장(중복 url 스킵)."""
    now = int(time.time())
    key = event["event_key"]
    c = conn()
    row = c.execute("SELECT id FROM kb_events WHERE event_key=?", (key,)).fetchone()
    fields = (
        event.get("scope_type") or "stock",
        event.get("ticker"),
        event.get("sector"),
        event["event_type"],
        event.get("direction") or "unknown",
        event.get("severity") or "info",
        float(event.get("confidence") or 1.0),
        event.get("trust_tier") or "official",
        event.get("status") or "confirmed",
        1 if event.get("decision_eligible") else 0,
        event.get("decision_action") or "none",
        int(event.get("detected_at") or now),
        event.get("effective_at"),
        event.get("expires_at"),
        event.get("resolved_at"),
        event.get("summary"),
        event.get("rationale"),
        event.get("extractor_model"),
        event.get("policy_version") or "p0",
    )
    if row:
        eid = row[0]
        c.execute(
            "UPDATE kb_events SET scope_type=?,ticker=?,sector=?,event_type=?,direction=?,"
            "severity=?,confidence=?,trust_tier=?,status=?,decision_eligible=?,decision_action=?,"
            "detected_at=?,effective_at=?,expires_at=?,resolved_at=?,summary=?,rationale=?,"
            "extractor_model=?,policy_version=?,updated=? WHERE id=?",
            (*fields, now, eid),
        )
    else:
        cur = c.execute(
            "INSERT INTO kb_events(event_key,scope_type,ticker,sector,event_type,direction,severity,"
            "confidence,trust_tier,status,decision_eligible,decision_action,detected_at,effective_at,"
            "expires_at,resolved_at,summary,rationale,extractor_model,policy_version,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, *fields, now, now),
        )
        eid = cur.lastrowid
    if evidence and evidence.get("url"):
        exists = c.execute(
            "SELECT id FROM kb_event_evidence WHERE event_id=? AND url=?",
            (eid, evidence["url"]),
        ).fetchone()
        if not exists:
            c.execute(
                "INSERT INTO kb_event_evidence(event_id,entry_id,source_key,url,published,"
                "evidence_text,support_role,trust_score,created) VALUES(?,?,?,?,?,?,?,?,?)",
                (eid, evidence.get("entry_id"), evidence.get("source_key"), evidence["url"],
                 evidence.get("published"), evidence.get("evidence_text"),
                 evidence.get("support_role") or "primary", evidence.get("trust_score"), now),
            )
    c.commit()
    c.close()
    return int(eid)


def kb_events_active(ticker: str | None = None, *, now: int | None = None,
                     decision_only: bool = False) -> list[dict]:
    """만료되지 않은 confirmed 이벤트. decision_only면 decision_eligible=1만."""
    ts = int(now if now is not None else time.time())
    c = conn()
    q = f"SELECT {_EVT_COLS} FROM kb_events WHERE status='confirmed' AND (expires_at IS NULL OR expires_at>=?)"
    args: list = [ts]
    if ticker:
        q += " AND ticker=?"; args.append(ticker)
    if decision_only:
        q += " AND decision_eligible=1"
    q += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'serious' THEN 1 WHEN 'watch' THEN 2 ELSE 3 END, detected_at DESC"
    rows = c.execute(q, args).fetchall()
    c.close()
    return [_evt_row(r) for r in rows]


def kb_events_list(limit: int = 50, ticker: str | None = None,
                   status: str | None = None) -> list[dict]:
    c = conn()
    q = f"SELECT {_EVT_COLS} FROM kb_events"
    where, args = [], []
    if ticker:
        where.append("ticker=?"); args.append(ticker)
    if status:
        where.append("status=?"); args.append(status)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY updated DESC LIMIT ?"; args.append(limit)
    rows = c.execute(q, args).fetchall()
    c.close()
    return [_evt_row(r) for r in rows]


def kb_event_queue_status(*, now: int | None = None, soon_hours: int = 24) -> dict:
    """사람 확인을 기다리는 후보 이벤트 큐. 후보는 EVENT_TTL_DAYS 뒤 자동 만료되므로
    아무도 안 보면 '오래된 악재로 매수를 막는' 사고는 없지만, 반대로 **유효한 악재가 조용히
    사라진다**. 큐 길이와 만료 임박 건수를 화면에 드러내야 그 손실이 보인다."""
    ts = int(now if now is not None else time.time())
    c = conn()
    rows = c.execute(
        "SELECT ticker, severity, summary, expires_at FROM kb_events "
        "WHERE status='candidate' AND (expires_at IS NULL OR expires_at>=?) "
        "ORDER BY expires_at ASC", (ts,)).fetchall()
    c.close()
    cutoff = ts + soon_hours * 3600
    soon = [{"ticker": r[0], "severity": r[1], "summary": r[2],
             "hours_left": (round((r[3] - ts) / 3600, 1) if r[3] else None)}
            for r in rows if r[3] and r[3] <= cutoff]
    # SLA: 만료 임박이 있거나 pending≥5면 관리자 즉시 처리 대상
    sla_alert = bool(soon) or len(rows) >= 5
    return {"pending": len(rows), "expiring_soon": len(soon), "soon_items": soon[:8],
            "soon_hours": soon_hours, "sla_alert": sla_alert,
            "note": ("만료 임박·적체 — 확인 안 하면 유효 악재가 사라집니다"
                     if sla_alert else "큐 안정")}


def kb_event_exists(event_key: str) -> bool:
    c = conn()
    row = c.execute("SELECT 1 FROM kb_events WHERE event_key=?", (event_key,)).fetchone()
    c.close()
    return row is not None


def kb_event_get(event_id: int) -> dict | None:
    c = conn()
    row = c.execute(f"SELECT {_EVT_COLS} FROM kb_events WHERE id=?", (int(event_id),)).fetchone()
    c.close()
    return _evt_row(row) if row else None


def kb_event_review(event_id: int, *, status: str, decision_eligible: bool,
                    decision_action: str, rationale_suffix: str | None = None) -> dict | None:
    """후보 이벤트 사람 검토 — status/eligible/action만 갱신. 없으면 None."""
    ev = kb_event_get(event_id)
    if not ev:
        return None
    now = int(time.time())
    rationale = ev.get("rationale") or ""
    if rationale_suffix:
        note = f" · {rationale_suffix}"
        if note not in rationale:
            rationale = (rationale + note).strip(" ·")[:400]
    c = conn()
    c.execute(
        "UPDATE kb_events SET status=?, decision_eligible=?, decision_action=?, "
        "rationale=?, updated=? WHERE id=?",
        (status, 1 if decision_eligible else 0, decision_action, rationale, now, int(event_id)),
    )
    c.commit()
    c.close()
    return kb_event_get(event_id)


def kb_event_evidence(event_id: int) -> list[dict]:
    c = conn()
    rows = c.execute(
        "SELECT id,event_id,entry_id,source_key,url,published,evidence_text,support_role,trust_score,created "
        "FROM kb_event_evidence WHERE event_id=? ORDER BY id", (event_id,),
    ).fetchall()
    c.close()
    cols = ["id", "event_id", "entry_id", "source_key", "url", "published", "evidence_text",
            "support_role", "trust_score", "created"]
    return [dict(zip(cols, r)) for r in rows]


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


# 같은 종목·같은 날 판단은 1건으로 센다. 성향이 다른 봇 3개가 같은 종목을 사면 같은 판단이
# 3번 기록되는데(실측: 39건 중 8건 중복, 161390은 하루 3건), 그대로 세면 승률이 **시그널 정확도가
# 아니라 종목 인기도**로 가중된다 — 세 봇이 다 산 종목이 오르면 3승, 하나만 산 종목이 오르면 1승이다.
_DECISION_DEDUP = ("SELECT MIN(id) FROM bot_decisions GROUP BY ticker, action, date(ts,'unixepoch')")


def bot_decisions_recent(limit: int = 40) -> list[dict]:
    """최근 판단(중복 제거). 성향별 봇이 같은 날 같은 종목을 사도 판단 자체는 하나다."""
    c = conn()
    rows = c.execute("SELECT ticker,name,action,score,rationale,context,decided_price,ts,outcome_pct,outcome_ts "
                     f"FROM bot_decisions WHERE id IN ({_DECISION_DEDUP}) "
                     "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [{"ticker": t, "name": n, "action": a, "score": sc, "rationale": r,
             "context": json.loads(cx or "{}"), "decided_price": dp, "ts": ts,
             "outcome_pct": op, "outcome_ts": ot}
            for t, n, a, sc, r, cx, dp, ts, op, ot in rows]


def bot_decision_scorecard() -> dict:
    """실현된 매수 판단 성적표. 미실현(outcome_pct NULL) 제외. 중복 판단은 1건으로 센다.

    **고정 지평(`horizon_days IS NOT NULL`)만 승률·리프트에 쓴다.** 2026-08-05 진단에서 옛 채점이
    `closes[-1]`(오늘 종가)을 써서 보유 기간이 판단마다 달랐다(3.0~6.1일). 지평이 섞인 비율은
    비교 대상이 없으므로 base rate 를 붙일 수 없다 — 섞인 건수는 `mixed_horizon_n` 으로 드러낸다.
    """
    c = conn()
    n, wins, avg, best, worst = c.execute(
        "SELECT COUNT(*), SUM(CASE WHEN outcome_pct>0 THEN 1 ELSE 0 END), "
        "AVG(outcome_pct), MAX(outcome_pct), MIN(outcome_pct) "
        f"FROM bot_decisions WHERE action='buy' AND outcome_pct IS NOT NULL "
        f"AND horizon_days IS NOT NULL AND id IN ({_DECISION_DEDUP})"
    ).fetchone()
    total = c.execute("SELECT COUNT(*) FROM bot_decisions WHERE action='buy' "
                      f"AND id IN ({_DECISION_DEDUP})").fetchone()[0] or 0
    dupes = c.execute("SELECT COUNT(*) FROM bot_decisions WHERE action='buy'").fetchone()[0] or 0
    mixed = c.execute("SELECT COUNT(*) FROM bot_decisions WHERE action='buy' "
                      "AND outcome_pct IS NOT NULL AND horizon_days IS NULL "
                      f"AND id IN ({_DECISION_DEDUP})").fetchone()[0] or 0
    hz = c.execute("SELECT DISTINCT horizon_days FROM bot_decisions WHERE action='buy' "
                   "AND horizon_days IS NOT NULL ORDER BY horizon_days").fetchall()
    days = c.execute("SELECT DISTINCT entry_date FROM bot_decisions WHERE action='buy' "
                     "AND outcome_pct IS NOT NULL AND horizon_days IS NOT NULL "
                     "AND entry_date IS NOT NULL ORDER BY entry_date").fetchall()
    c.close()
    n = n or 0
    return {"resolved": n, "pending": total - n,
            "win_rate": round(wins / n * 100, 1) if n else None,
            "avg_outcome_pct": round(avg, 2) if avg is not None else None,
            "best_pct": round(best, 2) if best is not None else None,
            "worst_pct": round(worst, 2) if worst is not None else None,
            # 중복이 몇 건 접혔는지 드러낸다 — 안 보이면 다시 중복으로 세는 코드가 생긴다.
            "deduped_from": dupes,
            # 지평이 섞인 옛 채점은 리프트에서 뺐다. 몇 건을 뺐는지 밝히지 않으면 표본이
            # 조용히 줄어든 것처럼 보인다.
            "mixed_horizon_n": mixed,
            "horizon_days": [int(r[0]) for r in hz],
            "entry_dates": [str(r[0]) for r in days]}


def bot_decision_set_outcome(decision_id: int, outcome_pct: float, *,
                             horizon_days: int = 3,
                             entry_date: str | None = None,
                             exit_date: str | None = None) -> None:
    """판단 한 건의 사후수익 확정. **지평을 반드시 남긴다.**

    `horizon_days`가 없는 행은 스코어카드가 리프트 계산에서 뺀다(비교 가능한 base rate 를 만들 수
    없으므로). 그래서 새 채점은 어느 경로로 들어와도 지평을 기록해야 한다 — 기본값을 두는 이유다.
    """
    c = conn()
    c.execute("UPDATE bot_decisions SET outcome_pct=?, outcome_ts=?, horizon_days=?, "
              "entry_date=?, exit_date=? WHERE id=?",
              (outcome_pct, int(time.time()), int(horizon_days), entry_date, exit_date, decision_id))
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


# ---------- hypo_runs (이슈 흐름 이력 — **append-only**) ----------
# 2026-08-07: `kv:hypo:v4:latest` **1슬롯 덮어쓰기**라 지난 흐름이 매번 파괴됐고, 그래서
# "지목한 업종이 그 뒤 실제로 어땠나"를 **한 번도 측정할 수 없었다**. `harness_last.json`
# 1슬롯으로 이미 겪은 병이다(그때도 정본이 마지막 칸이 됐다). 이력이 없으면 정확도도 없다.


def hypo_run_insert(row: dict) -> int:
    """이슈 흐름 한 건 적재. 덮어쓰지 않는다 — 채점은 과거 표본으로만 할 수 있다."""
    import json as _json
    with conn() as c:
        cur = c.execute(
            "INSERT INTO hypo_runs(built_at, as_of, source, model, sectors_json, tickers_json, tree_json)"
            " VALUES(?,?,?,?,?,?,?)",
            (row["built_at"], row.get("as_of"), row.get("source") or "rules", row.get("model"),
             _json.dumps(row.get("sectors") or [], ensure_ascii=False),
             _json.dumps(row.get("tickers") or [], ensure_ascii=False),
             _json.dumps(row.get("tree") or {}, ensure_ascii=False)))
        return int(cur.lastrowid)


def hypo_runs_recent(limit: int = 50) -> list[dict]:
    """최신순 이력. `tree_json` 은 무거우니 기본으로 파싱하지 않는다(채점은 sectors만 쓴다)."""
    import json as _json
    with conn() as c:
        rows = c.execute(
            "SELECT id, built_at, as_of, source, model, sectors_json, tickers_json"
            " FROM hypo_runs ORDER BY built_at DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["sectors"] = _json.loads(d.pop("sectors_json") or "[]")
        d["tickers"] = _json.loads(d.pop("tickers_json") or "[]")
        out.append(d)
    return out


# ---------- audit_hypotheses (감사 가설 큐 — 관측만, 엔진에 영향 없음) ----------
_AUDIT_COLS = ("id,target,title,claim,falsifier,check_hint,severity,status,note,"
               "created,reviewed")
AUDIT_STATUSES = ("pending", "promoted", "dismissed")


def _audit_row(r) -> dict:
    (hid, target, title, claim, falsifier, check_hint, severity, status, note,
     created, reviewed) = r
    return {"id": hid, "target": target, "title": title, "claim": claim,
            "falsifier": falsifier, "check_hint": check_hint, "severity": severity,
            "status": status, "note": note, "created": created, "reviewed": reviewed}


def audit_hypothesis_upsert(item: dict) -> None:
    """가설 upsert. id를 내용 해시로 만들어 같은 지적이 매주 쌓이지 않게 한다."""
    c = conn()
    c.execute(f"INSERT OR IGNORE INTO audit_hypotheses({_AUDIT_COLS}) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (item["id"], item.get("target"), item.get("title"), item.get("claim"),
               item.get("falsifier"), item.get("check_hint"),
               item.get("severity") or "medium", item.get("status") or "pending",
               item.get("note"), item.get("created") or int(time.time()),
               item.get("reviewed")))
    c.commit()
    c.close()


def audit_hypothesis_list(status: str | None = None, limit: int = 50) -> list[dict]:
    c = conn()
    if status:
        rows = c.execute(f"SELECT {_AUDIT_COLS} FROM audit_hypotheses WHERE status=? "
                         "ORDER BY created DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = c.execute(f"SELECT {_AUDIT_COLS} FROM audit_hypotheses "
                         "ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [_audit_row(r) for r in rows]


def audit_hypothesis_set_status(hid: str, status: str, note: str = "") -> bool:
    if status not in AUDIT_STATUSES:
        raise ValueError(f"unknown status: {status}")
    c = conn()
    cur = c.execute("UPDATE audit_hypotheses SET status=?, note=?, reviewed=? WHERE id=?",
                    (status, note or None, int(time.time()), hid))
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return ok


def audit_pending_count() -> int:
    c = conn()
    n = c.execute("SELECT COUNT(*) FROM audit_hypotheses WHERE status='pending'").fetchone()[0]
    c.close()
    return n


# ---------- brain_proposals (두뇌 개선 제안 큐 — 관리자 승인 후 엔진 반영) ----------
_BRAIN_PROP_COLS = ("id,kind,title,body_ko,rationale_ko,patch,baseline,evidence,"
                    "method_key,confidence,status,note,created,reviewed")


def _brain_proposal_row(r) -> dict:
    (pid, kind, title, body_ko, rationale_ko, patch, baseline, evidence,
     method_key, confidence, status, note, created, reviewed) = r
    return {"id": pid, "kind": kind, "title": title, "body_ko": body_ko,
            "rationale_ko": rationale_ko,
            "patch": json.loads(patch) if patch else {},
            "baseline": json.loads(baseline) if baseline else {},
            "evidence": json.loads(evidence) if evidence else {},
            "method_key": method_key, "confidence": confidence,
            "status": status, "note": note, "created": created, "reviewed": reviewed}


def brain_proposal_upsert(item: dict) -> None:
    """제안 upsert(동일 id면 덮어씀). draft 재생성·일별 멱등에 사용."""
    c = conn()
    c.execute(f"INSERT OR REPLACE INTO brain_proposals({_BRAIN_PROP_COLS}) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (item["id"], item.get("kind") or "weight_nudge",
               item.get("title"), item.get("body_ko"), item.get("rationale_ko"),
               json.dumps(item.get("patch") or {}, ensure_ascii=False),
               json.dumps(item.get("baseline") or {}, ensure_ascii=False),
               json.dumps(item.get("evidence") or {}, ensure_ascii=False),
               item.get("method_key"), item.get("confidence"),
               item.get("status", "draft"), item.get("note"),
               item.get("created") or int(time.time()), item.get("reviewed")))
    c.commit()
    c.close()


def brain_proposal_list(status: str | None = None, limit: int = 50) -> list[dict]:
    c = conn()
    if status:
        rows = c.execute(
            f"SELECT {_BRAIN_PROP_COLS} FROM brain_proposals WHERE status=? "
            "ORDER BY created DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = c.execute(
            f"SELECT {_BRAIN_PROP_COLS} FROM brain_proposals "
            "ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [_brain_proposal_row(r) for r in rows]


def brain_proposal_get(pid: str) -> dict | None:
    c = conn()
    r = c.execute(f"SELECT {_BRAIN_PROP_COLS} FROM brain_proposals WHERE id=?",
                  (pid,)).fetchone()
    c.close()
    return _brain_proposal_row(r) if r else None


def brain_proposal_set_status(pid: str, status: str, note: str = "") -> None:
    c = conn()
    c.execute("UPDATE brain_proposals SET status=?, note=?, reviewed=? WHERE id=?",
              (status, note or None, int(time.time()), pid))
    c.commit()
    c.close()


def brain_proposal_claim(pid: str, status: str, note: str = "") -> bool:
    """draft → approved|rejected 원자 전환. True=이 요청이 선점, False=이미 처리됨."""
    c = conn()
    cur = c.execute(
        "UPDATE brain_proposals SET status=?, note=?, reviewed=? "
        "WHERE id=? AND status='draft'",
        (status, note or None, int(time.time()), pid))
    c.commit()
    ok = cur.rowcount > 0
    c.close()
    return ok


def brain_proposal_draft_count() -> int:
    c = conn()
    n = c.execute("SELECT COUNT(*) FROM brain_proposals WHERE status='draft'").fetchone()[0]
    c.close()
    return int(n)


def brain_proposal_draft_for_factor(factor: str) -> dict | None:
    """동일 팩터의 미검토 draft가 있으면 반환 — refresh 시 중복 카드 방지."""
    c = conn()
    rows = c.execute(
        f"SELECT {_BRAIN_PROP_COLS} FROM brain_proposals WHERE status='draft' "
        "ORDER BY created DESC LIMIT 80").fetchall()
    c.close()
    for r in rows:
        item = _brain_proposal_row(r)
        if (item.get("evidence") or {}).get("factor") == factor:
            return item
    return None


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


# ---------- llm_usage (이 앱 LLM 호출 추정 비용 — 공유 키와 분리 집계) ----------
def llm_usage_add(*, model: str, kind: str, input_tokens: int, output_tokens: int,
                  cost_usd: float, ok: bool = True) -> None:
    c = conn()
    c.execute(
        "INSERT INTO llm_usage(ts,model,kind,input_tokens,output_tokens,cost_usd,ok) "
        "VALUES(?,?,?,?,?,?,?)",
        (int(time.time()), model, kind or "complete", int(input_tokens), int(output_tokens),
         float(cost_usd), 1 if ok else 0),
    )
    c.commit()
    c.close()


def llm_spend_usd(*, window_sec: int) -> float | None:
    """최근 `window_sec` 초 동안의 추정 LLM 지출(USD). 읽을 수 없으면 **None**.

    예산 게이트가 이 값을 본다. 0.0과 None을 구분하는 것이 핵심이다 — 0.0은 "안 썼다"이고
    None은 "모른다"이며, 모를 때 통과시키면 fail-open이라 게이트가 없는 것과 같다.
    """
    try:
        c = conn()
        row = c.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE ts>=? AND ok=1",
                        (int(time.time()) - max(1, int(window_sec)),)).fetchone()
        c.close()
        return float(row[0] or 0.0)
    except Exception:                            # noqa: BLE001 — 못 읽으면 막는 쪽이 안전하다
        return None


def llm_usage_summary(days: int = 30) -> dict:
    """기간별 호출·토큰·추정 USD + 모델별 분해. Anthropic 청구와 다를 수 있음(캐시·배치 미반영)."""
    now = int(time.time())
    since = now - max(1, int(days)) * 86400
    day_ago = now - 86400
    week_ago = now - 7 * 86400
    c = conn()
    rows = c.execute(
        "SELECT ts,model,kind,input_tokens,output_tokens,cost_usd FROM llm_usage "
        "WHERE ts>=? ORDER BY ts DESC",
        (since,),
    ).fetchall()
    c.close()
    total = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    today = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    week = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    by_model: dict[str, dict] = {}
    # 기능별 귀속(2026-08-07). `kind` 는 `기능:전송` 형태다 — 전송만 기록하던 동안
    # "월 $64 를 누가 썼나"를 알 수 없었다. 귀속 안 된 호출은 `unattributed` 로 **보이게** 둔다.
    by_purpose: dict[str, dict] = {}
    for ts, model, kind, inp, out, cost in rows:
        inp, out, cost = int(inp or 0), int(out or 0), float(cost or 0)
        total["calls"] += 1
        total["input_tokens"] += inp
        total["output_tokens"] += out
        total["cost_usd"] += cost
        if ts >= week_ago:
            week["calls"] += 1
            week["input_tokens"] += inp
            week["output_tokens"] += out
            week["cost_usd"] += cost
        if ts >= day_ago:
            today["calls"] += 1
            today["input_tokens"] += inp
            today["output_tokens"] += out
            today["cost_usd"] += cost
        m = by_model.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        m["calls"] += 1
        m["input_tokens"] += inp
        m["output_tokens"] += out
        m["cost_usd"] += cost
        # 옛 행은 `complete`/`tools`/`stream` 처럼 전송만 있다 → 기능 자리를 `legacy` 로 둔다.
        purpose = (str(kind or "").split(":", 1)[0]
                   if ":" in str(kind or "") else f"legacy({kind or '?'})")
        q = by_purpose.setdefault(purpose, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        q["calls"] += 1
        q["input_tokens"] += inp
        q["output_tokens"] += out
        q["cost_usd"] += cost
    for bucket in (total, today, week):
        bucket["cost_usd"] = round(bucket["cost_usd"], 4)
    for m in by_model.values():
        m["cost_usd"] = round(m["cost_usd"], 4)
    for q in by_purpose.values():
        q["cost_usd"] = round(q["cost_usd"], 4)
    models = sorted(by_model.items(), key=lambda x: -x[1]["cost_usd"])
    purposes = sorted(by_purpose.items(), key=lambda x: -x[1]["cost_usd"])
    return {
        "days": days,
        "since_ts": since,
        "total": total,
        "today": today,
        "last_7d": week,
        "by_model": [{"model": k, **v} for k, v in models],
        # 무엇을 줄일지 정하려면 **기능별**로 보여야 한다.
        "by_purpose": [{"purpose": k, **v} for k, v in purposes],
        "note": "추정 비용(공개 단가·캐시/배치 미반영). Anthropic 콘솔 청구와 다를 수 있음. 이 앱 호출분만 집계.",
    }


# ---------------------------------------------------------------- 하네스 판정 이력
#
# append-only. 이 아래에 UPDATE·DELETE 함수를 추가하지 말 것 — 덮어쓸 수 있는 판정은 증거가 아니다.
# docs/prd-harness-preregistration.md F5.

_HARNESS_RUN_COLS = (
    "id,ran_at,preregistered_id,score_source,market,config_json,config_hash,harness_json,"
    "percentile,threshold_pct,n_registered,periods,empty_periods,effective_periods,"
    "pit_dates,price_data_to,verdict,verdict_why,is_locked,warnings_json,note,sharpe_json")


def harness_run_insert(row: dict) -> int:
    """판정 실행 1건을 이력에 남긴다. `preregistered_id`가 없으면 `is_locked`는 강제로 0이다.

    잠금(정본 확정)을 호출자 판단에 맡기지 않는 이유: 탐색 실행이 우연히 문턱을 넘었을 때
    "이건 정본으로 하자"가 되는 경로를 코드에서 없애야 한다. 사전등록이 없으면 잠기지 않는다.
    """
    locked = int(bool(row.get("is_locked"))) if row.get("preregistered_id") else 0
    c = conn()
    cur = c.execute(
        "INSERT INTO harness_runs(ran_at,preregistered_id,score_source,market,config_json,"
        "config_hash,harness_json,percentile,threshold_pct,n_registered,periods,empty_periods,"
        "effective_periods,pit_dates,price_data_to,verdict,verdict_why,is_locked,warnings_json,"
        "note,sharpe_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row.get("ran_at") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
         row.get("preregistered_id"), row.get("score_source") or "price",
         row.get("market") or "kr", row.get("config_json") or "{}",
         row.get("config_hash") or "", row.get("harness_json") or "{}",
         row.get("percentile"), row.get("threshold_pct"), row.get("n_registered"),
         row.get("periods"), row.get("empty_periods"), row.get("effective_periods"),
         row.get("pit_dates"), row.get("price_data_to"),
         row.get("verdict"), row.get("verdict_why"), locked,
         row.get("warnings_json") or "[]", row.get("note"),
         (json.dumps(row["sharpe"]) if row.get("sharpe") is not None else None)))
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


def harness_run_get(rid: int) -> dict | None:
    c = conn()
    r = c.execute(f"SELECT {_HARNESS_RUN_COLS} FROM harness_runs WHERE id=?", (rid,)).fetchone()
    c.close()
    return dict(zip(_HARNESS_RUN_COLS.split(","), r)) if r else None


def harness_runs_recent(limit: int = 20) -> list[dict]:
    c = conn()
    rows = c.execute(f"SELECT {_HARNESS_RUN_COLS} FROM harness_runs "
                     "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    c.close()
    cols = _HARNESS_RUN_COLS.split(",")
    return [dict(zip(cols, r)) for r in rows]


def harness_trial_counts(*, market: str | None = None) -> dict:
    """**시도 횟수 집계(L4)** — 지금까지 몇 개의 서로 다른 설정을 돌려봤나.

    이 리포의 핵심 실패 모드는 고르기다: *"8개 조합을 한 번에 보면 판별력이 전혀 없어도 그중
    하나가 95%를 넘을 확률이 34%"*. 사전등록 Šidák은 **같은 가설을 몇 번 볼지**를 막지만,
    그 앞단의 **탐색으로 몇 개 조합을 돌려봤나**는 보정되지 않았다. `harness_runs`가 append-only로
    전부 남아 있으므로 세면 된다 — Deflated Sharpe(L3)가 이 수를 N으로 쓴다.

    `distinct_configs`는 (설정 해시 + 하네스 파라미터) 조합의 고유 수다. 같은 조합을 여러 번
    돌린 것은 새 시도가 아니다(재현이다). 반대로 가중치 하나만 달라도 새 시도로 센다.
    """
    c = conn()
    where, args = "", []
    if market:
        where, args = " WHERE market=?", [market]
    rows = c.execute(
        f"SELECT config_hash, harness_json, score_source, preregistered_id, ran_at, sharpe_json "
        f"FROM harness_runs{where}", args).fetchall() if _has_sharpe_col(c) else c.execute(
        f"SELECT config_hash, harness_json, score_source, preregistered_id, ran_at, NULL "
        f"FROM harness_runs{where}", args).fetchall()
    c.close()
    combos: dict[tuple, int] = {}
    prereg_ids: set[str] = set()
    first = last = None
    for chash, hjson, src, pid, ran_at, _ in rows:
        key = (chash, hjson, src)
        combos[key] = combos.get(key, 0) + 1
        if pid:
            prereg_ids.add(pid)
        first = ran_at if first is None or (ran_at or "") < first else first
        last = ran_at if last is None or (ran_at or "") > last else last
    return {
        "runs": len(rows),
        "distinct_configs": len(combos),
        "repeats": len(rows) - len(combos),
        "preregistered_ids": sorted(prereg_ids),
        "first_run_at": first, "last_run_at": last,
        # 바꿀 수 있는 파라미터 수 — 탐색 공간의 크기를 가늠하게 한다(시도 수와는 별개).
        "tunable_params": len(_tunable_param_names()),
        "param_names": _tunable_param_names(),
    }


def _tunable_param_names() -> list[str]:
    from signal_desk import signalcfg
    return [*signalcfg.FIELDS, signalcfg.MODE_FIELD]


def _has_sharpe_col(c) -> bool:
    return "sharpe_json" in {r[1] for r in c.execute("PRAGMA table_info(harness_runs)")}


def harness_sharpes(*, market: str | None = None) -> list[float]:
    """시도별 기간 Sharpe 목록 — DSR의 `sr_variance`(시도 간 Sharpe 분산) 실측용.

    이론값 1/(T−1)로 대체할 수도 있지만, 실측 분산이 있으면 그쪽이 정직하다. 없으면
    `deflated_sharpe`가 근사를 썼다고 `sr_variance_source`에 남긴다.
    """
    c = conn()
    if not _has_sharpe_col(c):
        c.close()
        return []
    where, args = ("", []) if not market else (" WHERE market=?", [market])
    rows = c.execute(f"SELECT sharpe_json FROM harness_runs{where}", args).fetchall()
    c.close()
    out = []
    for (raw,) in rows:
        try:
            v = json.loads(raw or "null")
        except Exception:                            # noqa: BLE001
            continue
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def harness_locked_run(preregistered_id: str) -> dict | None:
    """그 look의 **최초** 확정 실행. 요건 충족일 1회 판정이므로 가장 이른 것이 정본이다."""
    c = conn()
    r = c.execute(f"SELECT {_HARNESS_RUN_COLS} FROM harness_runs "
                  "WHERE preregistered_id=? AND is_locked=1 ORDER BY id ASC LIMIT 1",
                  (preregistered_id,)).fetchone()
    c.close()
    return dict(zip(_HARNESS_RUN_COLS.split(","), r)) if r else None
