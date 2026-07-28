"""장중 시그널 전이 로그 — 일별 PIT와 별개.

quote 루프마다 전체를 찍지 않고, kind가 바뀔 때만 kv에 남긴다.
- first_buy_*: 당일 매수권 에피소드 시작(시각·가격)
- demoted_*: 매수권 → 비매수 전이(급락·악재 등)

마감 PIT(`signal_history`)는 백테스트 정본. 이건 UX·진입품질 당일 보정용.
"""

from __future__ import annotations

import time
from typing import Any

from signal_desk import db
from signal_desk.signals.engine import is_buy

_DEMOTE_TAGS = ("급락", "악재", "실적", "추세", "게이트", "매수권밖")


def _key(market: str, today: str) -> str:
    return f"sig_ep:{market}:{today}"


def load(market: str, today: str) -> dict[str, dict]:
    raw = db.kv_get(_key(market, today))
    return raw if isinstance(raw, dict) else {}


def demote_reason(row: dict) -> str | None:
    tag = row.get("hold_tag")
    if tag in _DEMOTE_TAGS:
        return str(tag)
    if row.get("event_risk") or row.get("decision_buy_blocked"):
        return "악재"
    reasons = " ".join(row.get("reasons") or [])
    if "[급락]" in reasons:
        return "급락"
    if "[악재]" in reasons:
        return "악재"
    if "[실적]" in reasons:
        return "실적"
    return "해제"


def observe_rows(
    rows: list[dict],
    *,
    market: str,
    today: str,
    now_ts: int | None = None,
) -> dict[str, dict]:
    """kind 전이만 upsert. 반환: 저장 후 전체 state."""
    now_ts = int(now_ts if now_ts is not None else time.time())
    state = load(market, today)
    changed = False
    for r in rows:
        t = r.get("ticker")
        if not t:
            continue
        kind = str(r.get("kind") or "")
        prev = state.get(t) or {}
        prev_kind = str(prev.get("last_kind") or "")
        if kind == prev_kind and prev:
            continue
        cur: dict[str, Any] = {
            "last_kind": kind,
            "updated_at": now_ts,
        }
        # 이전 에피소드 필드 유지(당일)
        for k in ("first_buy_ts", "first_buy_px", "demoted_at", "demote_reason"):
            if k in prev:
                cur[k] = prev[k]

        was_buy, now_buy = is_buy(prev_kind), is_buy(kind)
        if now_buy and not was_buy:
            # 새 매수 에피소드 — 발동 시각·가 고정
            cur["first_buy_ts"] = now_ts
            px = r.get("price")
            try:
                fpx = float(px) if px is not None else None
            except (TypeError, ValueError):
                fpx = None
            if fpx and fpx > 0:
                cur["first_buy_px"] = round(fpx, 4)
            cur.pop("demoted_at", None)
            cur.pop("demote_reason", None)
        elif was_buy and not now_buy:
            cur["demoted_at"] = now_ts
            cur["demote_reason"] = demote_reason(r)

        state[t] = cur
        changed = True
    if changed:
        db.kv_set(_key(market, today), state)
    return state


def annotate_rows(
    rows: list[dict],
    *,
    market: str,
    today: str,
    state: dict[str, dict] | None = None,
) -> list[dict]:
    """rows에 episode 요약 + 당일 entry fire_price 보정."""
    state = state if state is not None else load(market, today)
    for r in rows:
        t = r.get("ticker")
        ep = state.get(t) if t else None
        if not ep:
            r["episode"] = None
            continue
        out = {
            "first_buy_ts": ep.get("first_buy_ts"),
            "first_buy_px": ep.get("first_buy_px"),
            "demoted_at": ep.get("demoted_at"),
            "demote_reason": ep.get("demote_reason"),
        }
        r["episode"] = out if any(v is not None for v in out.values()) else None
        entry = r.get("entry")
        if not entry or not out.get("first_buy_px"):
            continue
        if str(entry.get("fire_date") or "")[:10] != str(today)[:10]:
            continue
        try:
            fpx = float(out["first_buy_px"])
            px = float(r["price"]) if r.get("price") is not None else None
        except (TypeError, ValueError):
            continue
        if fpx <= 0 or px is None or px <= 0:
            continue
        entry["fire_price"] = round(fpx, 4)
        entry["fire_ts"] = out.get("first_buy_ts")
        entry["run_up_pct"] = round((px / fpx - 1.0) * 100.0, 1)
    return rows
