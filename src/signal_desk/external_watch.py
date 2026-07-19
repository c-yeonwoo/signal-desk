"""조사 후보 워치리스트 — 수동으로 넣은 종목을 조사 큐로만 보관.

외부 소스 크롤링 없음. 시그널 점수·가중치 미반영. KB 우선 수집 타깃으로만 쓴다.
"""

from __future__ import annotations

import datetime
import logging
import re
import time
from typing import Any
from zoneinfo import ZoneInfo

from signal_desk import db
from signal_desk.reference import us_ko

log = logging.getLogger("signal_desk.external_watch")

_KV_KEY = "external_watch:v1"
_MAX_ITEMS = 80
_KB_PRIORITY_CAP = 15  # 하루·1회 refresh에 앞에 붙일 상한


def _kst_now() -> str:
    return datetime.datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")


def _universe_maps() -> tuple[dict[str, str], dict[str, str]]:
    """ticker→name (KR), ticker→name (US 한글명 우선)."""
    from signal_desk import store
    kr = {u["ticker"]: u["name"] for u in (store.load_universe() or [])}
    us = {}
    for u in (store.load_us_universe() or []):
        tk = u["ticker"]
        us[tk] = us_ko.name_ko(tk, u.get("name") or tk)
    return kr, us


def _resolve_token(token: str, *, kr: dict[str, str], us: dict[str, str]) -> dict | None:
    """티커 또는 종목명 → {ticker, name, market}."""
    t = (token or "").strip()
    if not t:
        return None
    # 코드
    if t in kr:
        return {"ticker": t, "name": kr[t], "market": "KR"}
    up = t.upper()
    if up in us:
        return {"ticker": up, "name": us[up], "market": "US"}
    # 6자리 숫자
    if re.fullmatch(r"\d{6}", t) and t in kr:
        return {"ticker": t, "name": kr[t], "market": "KR"}
    # 이름 매칭 (정확·부분)
    for tk, name in kr.items():
        if name == t or t in name:
            return {"ticker": tk, "name": name, "market": "KR"}
    for tk, name in us.items():
        if name == t or t.upper() == tk or t in name:
            return {"ticker": tk, "name": name, "market": "US"}
    return None


def list_items() -> list[dict]:
    raw = db.kv_get(_KV_KEY) or []
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict) and x.get("ticker")]


def _save(items: list[dict]) -> None:
    db.kv_set(_KV_KEY, items[:_MAX_ITEMS])


def ticker_set() -> set[str]:
    return {x["ticker"] for x in list_items()}


def kb_priority_targets(limit: int = _KB_PRIORITY_CAP) -> list[dict]:
    """KB refresh용 [{ticker, name}] — 최근 추가분 우선."""
    items = list_items()
    out, seen = [], set()
    for it in items:
        tk = it.get("ticker")
        if not tk or tk in seen:
            continue
        out.append({"ticker": tk, "name": it.get("name") or tk})
        seen.add(tk)
        if len(out) >= limit:
            break
    return out


def add_items(raw_lines: str | list[str], *, source: str = "manual",
              note: str = "", url: str = "") -> dict:
    """여러 줄 붙여넣기 추가. 줄: '005930' / '삼성전자' / '005930 삼성전자' / 'AAPL'."""
    if isinstance(raw_lines, str):
        lines = [ln.strip() for ln in raw_lines.splitlines() if ln.strip()]
    else:
        lines = [str(x).strip() for x in raw_lines if str(x).strip()]
    src = (source or "manual").strip()[:32] or "manual"
    note = (note or "").strip()[:240]
    url = (url or "").strip()[:300]
    kr, us = _universe_maps()
    existing = list_items()
    by_tk = {x["ticker"]: x for x in existing}
    added, skipped, unresolved = [], [], []
    now = _kst_now()
    ts = int(time.time())

    for ln in lines:
        parts = ln.replace(",", " ").split()
        resolved = None
        # 첫 토큰이 티커면 우선
        if parts:
            resolved = _resolve_token(parts[0], kr=kr, us=us)
        if not resolved and len(parts) >= 2:
            resolved = _resolve_token(" ".join(parts[1:]), kr=kr, us=us)
        if not resolved:
            resolved = _resolve_token(ln, kr=kr, us=us)
        if not resolved:
            unresolved.append(ln)
            continue
        tk = resolved["ticker"]
        if tk in by_tk:
            # 메타만 갱신
            prev = by_tk[tk]
            prev["source"] = src
            if note:
                prev["note"] = note
            if url:
                prev["url"] = url
            prev["updated_at"] = now
            skipped.append(tk)
            continue
        row = {
            "ticker": tk,
            "name": resolved["name"],
            "market": resolved["market"],
            "source": src,
            "note": note,
            "url": url,
            "added_at": now,
            "updated_at": now,
            "ts": ts,
            "kb_collected_at": None,
        }
        existing.insert(0, row)
        by_tk[tk] = row
        added.append(tk)
        ts -= 1  # 순서 안정

    _save(existing)
    return {
        "ok": True,
        "added": added,
        "updated": skipped,
        "unresolved": unresolved[:20],
        "total": len(list_items()),
    }


def remove(ticker: str) -> dict:
    tk = (ticker or "").strip()
    drop = {tk, tk.upper()}
    items = [x for x in list_items() if x.get("ticker") not in drop]
    _save(items)
    return {"ok": True, "total": len(items)}


def clear() -> dict:
    _save([])
    return {"ok": True, "total": 0}


def mark_kb_collected(tickers: list[str]) -> None:
    want = set(tickers)
    now = _kst_now()
    items = list_items()
    for it in items:
        if it.get("ticker") in want:
            it["kb_collected_at"] = now
    _save(items)


def status() -> dict[str, Any]:
    items = list_items()
    with_kb = sum(1 for x in items if x.get("kb_collected_at"))
    return {
        "ready": True,
        "total": len(items),
        "with_kb": with_kb,
        "kb_priority_cap": _KB_PRIORITY_CAP,
        "max_items": _MAX_ITEMS,
        "items": items,
        "disclaimer": "직접 넣은 종목의 조사 대기열입니다. 시그널·매수 권유가 아니며 엔진 점수에 가산되지 않습니다.",
    }
