"""페이퍼 봇 체결 알림의 선별·표시. 실계좌 주문/개인 잔고를 다루지 않는다."""

from __future__ import annotations

import datetime
import hashlib
import math
from zoneinfo import ZoneInfo

from signal_desk import config

STYLE_LABELS = {"conservative": "안정형", "balanced": "균형형", "aggressive": "공격형"}
REASONS = {
    "SIGNAL": {"BUY": "신규 시그널", "SELL": "매도 시그널"},
    "ADD": {"BUY": "분할 추가매수"},
    "ROTATE_IN": {"BUY": "더 강한 종목으로 교체"},
    "ROTATE_OUT": {"SELL": "보유 종목 교체"},
    "STOP_LOSS": {"SELL": "손절"},
    "TAKE_PROFIT": {"SELL": "목표 수익 도달"},
    "TRAILING": {"SELL": "고점 대비 하락으로 이익 보호"},
    "EVENT": {"SELL": "확인된 악재로 전량 청산"},
    "EVENT_TRIM": {"SELL": "확인된 악재로 일부 축소"},
    "RESERVATION": {"BUY": "예약 목표가 도달"},
}
_KST = ZoneInfo("Asia/Seoul")


def selected(uid: int, reference_bots: dict[int, str]) -> bool:
    """공용 채팅방에는 설정된 한 성향만 푸시한다. all은 명시적으로 설정해야 한다."""
    style = reference_bots.get(uid)
    wanted = config.telegram_trade_style()
    return bool(style and wanted != "off" and (wanted == "all" or wanted == style))


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) else None


def _clean(value: object, limit: int = 48) -> str:
    return " ".join(str(value or "").split())[:limit]


def trade_rows(result: dict) -> list[dict]:
    """계획이나 실패를 체결로 포장하지 않는다. paper의 성공·주문번호·체결가가 모두 필요하다."""
    rows = []
    for side, key in (("SELL", "sells"), ("BUY", "buys")):
        for item in result.get(key) or []:
            if not isinstance(item, dict) or item.get("ok") is not True or not item.get("order_no"):
                continue
            qty, price = _number(item.get("qty")), _number(item.get("fill_price"))
            if qty is None or qty <= 0 or qty != int(qty) or price is None or price <= 0:
                continue
            rows.append({"side": side, "name": _clean(item.get("name") or item.get("ticker")),
                         "ticker": _clean(item.get("ticker"), 20), "qty": int(qty),
                         "price": price, "reason": item.get("reason"), "score": item.get("score"),
                         "order_no": str(item["order_no"])})
    return rows


def reservation_rows(result: dict | None) -> list[dict]:
    if not isinstance(result, dict) or not result.get("ok"):
        return []
    rows = []
    for item in result.get("executed") or []:
        if not isinstance(item, dict) or item.get("status") != "filled" or not item.get("order_no"):
            continue
        qty, price = _number(item.get("qty")), _number(item.get("fill_price"))
        if qty is None or qty <= 0 or qty != int(qty) or price is None or price <= 0:
            continue
        rows.append({"side": "BUY", "name": _clean(item.get("name") or item.get("ticker")),
                     "ticker": _clean(item.get("ticker"), 20), "qty": int(qty),
                     "price": price, "reason": "RESERVATION", "score": None,
                     "order_no": str(item["order_no"])})
    return rows


def dedupe_key(uid: int, market: str, rows: list[dict]) -> str:
    ids = sorted(f'{r["side"]}:{r["order_no"]}' for r in rows)
    return f'paper-fill:{uid}:{market}:' + hashlib.sha256("|".join(ids).encode()).hexdigest()[:24]


def render(style: str, market: str, rows: list[dict], *, total_eval: object = None,
           cash: object = None, now: datetime.datetime | None = None) -> str:
    """선택 봇·체결가·금액·이유를 한 화면에. 개인 실계좌 상태와 매수 권유는 넣지 않는다."""
    at = (now or datetime.datetime.now(_KST)).astimezone(_KST)
    label = "국내" if market == "kr" else "미국"
    unit = "원" if market == "kr" else "$"
    decimals = 0 if market == "kr" else 2
    lines = [f'🤖 페이퍼 체결 · {STYLE_LABELS.get(style, style)} · {label} · {at:%m/%d %H:%M} KST']
    for row in rows[:6]:
        action = "매수" if row["side"] == "BUY" else "매도"
        emoji = "🟢" if row["side"] == "BUY" else "🔴"
        price = f'{row["price"]:,.{decimals}f}'
        amount = f'{row["price"] * row["qty"]:,.{decimals}f}'
        lines.append(f'{emoji} {action} {row["name"]}({row["ticker"]}) {row["qty"]}주 × {price}{unit} ≈ {amount}{unit}')
        reason = REASONS.get(row["reason"], {}).get(row["side"], "전략 조건 충족")
        score = _number(row.get("score"))
        score_text = f' · 점수 {score:+.2f}' if score is not None else ""
        lines.append(f'   이유: {reason}{score_text}')
    if len(rows) > 6:
        lines.append(f'… 외 {len(rows) - 6}건은 앱에서 확인')
    total, available = _number(total_eval), _number(cash)
    if total is not None and total >= 0 and available is not None and available >= 0:
        lines.append(f'봇 평가액 {total:,.{decimals}f}{unit} · 현금 {available:,.{decimals}f}{unit}')
    lines.append("실제 계좌 주문 아님 · 모의 체결 기록")
    app_url = config.public_base_url()
    if app_url:
        lines.append(f'앱에서 보기 → {app_url}/#trading/live')
    return "\n".join(lines)
