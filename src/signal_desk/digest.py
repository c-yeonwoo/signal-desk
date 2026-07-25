"""아침 정기 브리핑 — 텔레그램 채널로 하루 1회(평일). 맥락 요약이고 매수 권유가 아니다.

「오늘은 시그널이 없어도 시장을 이렇게 읽는다」를 앱을 열지 않아도 받게 하는 D7 레일.
매수 0일에도 보낼 내용이 있어야 하므로, 매수 목록이 비면 **왜 비었는지**(문턱 상향·국면)와
매수 근접 종목을 대신 싣는다.

텍스트 조립은 순수 함수로 유지한다(입력=시그널·국면·문턱·실측). 발송·스케줄은 api가 맡는다.
"""

from __future__ import annotations

import datetime
from typing import Any

from signal_desk.signals.engine import is_buy

_WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")

NEAR_GAP = 0.5      # 문턱까지 이 이내면 '근접'(신뢰 스트립·매수 대기와 같은 기준)
MATURE_N = 20       # 실측 헤드라인 최소 표본(accuracy.ic_min_samples와 맞춤)
_BUY_LIMIT = 5
_NEAR_LIMIT = 3

DISCLAIMER = "맥락 요약입니다 · 매수 권유가 아닙니다"


def _date_line(d: datetime.date) -> str:
    return f"{d.month}/{d.day}({_WEEKDAY_KO[d.weekday()]})"


def _accuracy_line(accuracy: dict | None) -> str:
    """실측은 신뢰 스트립과 같은 가드 — 표본이 차기 전엔 숫자를 쓰지 않는다."""
    if not accuracy or not accuracy.get("ready"):
        return "실측: track record 쌓는 중"
    need = int(accuracy.get("ic_min_samples") or MATURE_N)
    matured = int((accuracy.get("coverage") or {}).get("matured_primary") or 0)
    if matured < need:
        return f"실측: 누적중 · 성숙 {matured}/{need}"
    prec = accuracy.get("buy_precision_pct")
    if prec is None:
        return f"실측: 누적중 · 성숙 {matured}/{need}"
    h = accuracy.get("primary_horizon") or 20
    line = f"실측 매수 정밀도 {prec:.1f}% ({h}거래일 · 표본 {accuracy.get('buy_sample') or matured})"
    # 기준선을 같이 적지 않으면 하락장에서 정밀도 숫자가 반대로 읽힌다
    lift = accuracy.get("buy_lift_pp")
    base = (accuracy.get("baseline") or {}).get("up_pct")
    if lift is not None and base is not None:
        line += f"\n기준선 {base:.1f}% · 리프트 {lift:+.1f}%p"
    return line


def _threshold_line(threshold: float, base_threshold: float, reasons: list[str] | None) -> str:
    bump = round(float(threshold) - float(base_threshold), 2)
    line = f"매수문턱 {threshold:.2f}"
    if bump > 0:
        line += f" (기본 {base_threshold:.2f} + 상향 {bump:.2f})"
    rs = [r for r in (reasons or []) if r]
    if rs:
        line += f"\n상향 사유: {' · '.join(rs[:3])}"
    return line


def _buyable(sig: Any) -> bool:
    """게이트에 막힌 종목은 매수권으로 세지 않는다(앱 리스트와 같은 기준)."""
    if getattr(sig, "event_risk", False):
        return False
    dec = getattr(sig, "decision", None)
    return not (dec is not None and getattr(dec, "buy_blocked", False))


def buy_signals(signals: list[Any]) -> list[Any]:
    """브리핑이 '매수 시그널'로 세는 것과 같은 목록(점수 내림차순).
    전일 대비 증감을 저장·비교하는 쪽에서도 같은 정의를 써야 하므로 밖으로 뺐다."""
    return sorted([s for s in signals if is_buy(getattr(s, "kind", "")) and _buyable(s)],
                  key=lambda s: float(s.score), reverse=True)


def _change_note(count: int, prev: int | None) -> str:
    """어제와 같은 숫자만 매일 보내면 3일 차에 열지 않는다 — 변화를 먼저 보여준다."""
    if prev is None or prev == count:
        return ""
    return f" (어제 {prev} → {count - prev:+d})"


def build_morning(
    *,
    signals: list[Any],
    regime_label: str | None,
    threshold: float,
    base_threshold: float,
    bump_reasons: list[str] | None = None,
    accuracy: dict | None = None,
    date: datetime.date | None = None,
    app_url: str | None = None,
    prev_buy_count: int | None = None,
) -> str:
    """아침 브리핑 본문. signals는 SignalResult 리스트(국내), threshold는 국면 반영 유효 문턱.

    app_url이 있으면 마지막에 「앱에서 보기」 링크를 붙인다 — 링크가 없으면 브리핑을 읽고
    끝나서 D7(재방문)에 구조적으로 기여하지 못한다.
    prev_buy_count가 있으면 매수 종목 수의 전일 대비 증감을 함께 적는다(매일 같은 문장 방지).
    """
    d = date or datetime.date.today()
    buys = buy_signals(signals)
    bought = {s.ticker for s in buys}
    near = sorted(
        [s for s in signals
         if s.ticker not in bought and _buyable(s) and 0 <= (threshold - float(s.score)) <= NEAR_GAP],
        key=lambda s: float(s.score), reverse=True,
    )

    lines = [f"☀️ {_date_line(d)} 아침 브리핑", ""]
    zone = f"시장 ZONE {regime_label}" if regime_label else "시장 ZONE 판정 없음"
    lines.append(f"{zone} · {_threshold_line(threshold, base_threshold, bump_reasons)}")
    lines.append("")

    change = _change_note(len(buys), prev_buy_count)
    if buys:
        lines.append(f"매수 시그널 {len(buys)}{change}")
        for s in buys[:_BUY_LIMIT]:
            lines.append(f"🟢 {s.name} {float(s.score):+.2f}")
        if len(buys) > _BUY_LIMIT:
            lines.append(f"… 외 {len(buys) - _BUY_LIMIT}종목")
    else:
        # 매수 0일이 정상 동작임을 매번 같은 문장으로 — 앱의 '매수 0일 히어로'와 같은 톤
        lines.append(f"매수 시그널 0{change} — 기준을 넘은 종목이 없습니다. 정밀도 우선이라 그렇고,"
                     " 고장이 아닙니다. 오늘은 기다리는 날입니다.")

    if near:
        lines.append("")
        lines.append(f"매수 근접(문턱까지 {NEAR_GAP:.1f} 이내) {len(near)}")
        for s in near[:_NEAR_LIMIT]:
            gap = threshold - float(s.score)
            lines.append(f"· {s.name} {float(s.score):+.2f} ({gap:.2f} 남음)")

    lines += ["", _accuracy_line(accuracy)]
    if app_url:
        # 딥링크는 시그널 탭으로 — D7 계측 지점(GET /api/signals)과 같은 화면이어야 한다.
        lines += ["", f"앱에서 보기 → {app_url.rstrip('/')}/#signal"]
    lines.append(DISCLAIMER)
    return "\n".join(lines)
