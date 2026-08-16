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


def _selection_line(selection: dict | None, exposure: float | None,
                    exposure_reasons: list[str] | None) -> str:
    """분위 모드 헤더 — 매수권은 상대 순위, 국면은 크기(익스포저)."""
    n = (selection or {}).get("universe") or 0
    slots = (selection or {}).get("rank_slots") or 0
    line = f"매수권 상위 {slots}종목/{n}"
    cut = (selection or {}).get("cutoff_score")
    if cut is not None:
        line += f" (컷오프 {cut:+.2f})"
    if exposure is not None:
        line += f" · 익스포저 {exposure * 100:.0f}%"
    rs = [r for r in (exposure_reasons or []) if r]
    if rs:
        line += f"\n익스포저 사유: {' · '.join(rs[:3])}"
    return line


def _picks_lines(signals: list[Any], *, selection: dict | None, threshold: float,
                 prev_buy_count: int | None) -> list[str]:
    """매수 + 근접 블록. **국내·미국이 같은 함수를 쓴다.**

    두 시장을 따로 조립하면 한쪽만 고쳐져 문구·기준이 갈라지고, 그 차이는 어느 화면에도
    안 뜬다(봇과 화면이 서로 다른 입력으로 점수를 조립하던 병과 같다).
    """
    ranked = (selection or {}).get("mode") == "rank"
    buys = buy_signals(signals)
    bought = {s.ticker for s in buys}
    # 분위 모드의 '근접'은 컷오프(매수권 막차 점수) 기준 — 절대 문턱은 더 이상 매수를 정하지 않는다
    near_ref = (selection or {}).get("cutoff_score") if ranked else threshold
    near = sorted(
        [s for s in signals
         if s.ticker not in bought and _buyable(s) and near_ref is not None
         and 0 <= (near_ref - float(s.score)) <= NEAR_GAP],
        key=lambda s: float(s.score), reverse=True,
    ) if near_ref is not None else []

    change = _change_note(len(buys), prev_buy_count)
    out: list[str] = []
    if buys:
        out.append(f"매수 시그널 {len(buys)}{change}")
        for s in buys[:_BUY_LIMIT]:
            out.append(f"🟢 {s.name} {float(s.score):+.2f}")
        if len(buys) > _BUY_LIMIT:
            out.append(f"… 외 {len(buys) - _BUY_LIMIT}종목")
    else:
        # 분위·절대 모두 매수 0일이 정상 — 최소점수·게이트로 자리가 비는 날이 신뢰의 본체
        out.append(f"매수 시그널 0{change} — 기준을 넘은 종목이 없습니다. 정밀도 우선이라 그렇고,"
                   " 고장이 아닙니다. 오늘은 기다리는 날입니다.")

    if near:
        out.append("")
        label = "컷오프" if ranked else "문턱"
        out.append(f"매수 근접({label}까지 {NEAR_GAP:.1f} 이내) {len(near)}")
        for s in near[:_NEAR_LIMIT]:
            out.append(f"· {s.name} {float(s.score):+.2f} ({near_ref - float(s.score):.2f} 남음)")
    return out


_STALL_NAMES = 3        # 이름을 다 적으면 브리핑이 길어진다. 3개 + "외 N개".


def stall_line(stall: dict | None) -> str | None:
    """수집 정지 한 줄. **브리핑 첫 줄**에 온다 — 아래로 밀면 안 읽힌다.

    이 레포에서 같은 병이 네 번 재발했다(시세 3주 정지 · `warnings.json` 부재 · 소스 4종이 수동
    버튼 전용 · `compute_quality` 호출 누락). 네 번 다 "수집 코드는 있는데 아무도 안 불렀다"였고,
    네 번 다 **화면에 안 떠서** 몇 주씩 몰랐다. 그래서 진단이 아니라 **알림**으로 만든다.

    정상일 때는 아무것도 반환하지 않는다(None) — 매일 초록불을 쓰면 그것도 안 읽히게 된다.
    """
    if not stall or stall.get("ok"):
        return None
    bits: list[str] = []
    missing = stall.get("missing_files") or []
    if missing:
        bits.append("파일 없음 " + " · ".join(missing[:_STALL_NAMES])
                    + (f" 외 {len(missing) - _STALL_NAMES}개" if len(missing) > _STALL_NAMES else ""))
    # **`updated` 가 없는 항목을 버리면 안 된다.** 예전엔 `if e.get("updated")` 로 걸렀는데,
    # 파생값(퀄리티)은 자기 파일이 없어 날짜가 None이라 **stale 목록에 있는데도 문장에서
    # 통째로 사라졌다**(실측: `stale=[us_prices, quality]` 인데 배너는 미국 시세만 말했다).
    # 파일 날짜가 없는 고장은 원리적으로 알릴 수 없는 구조였고, 그건 조용한 0이다.
    stale = list(stall.get("stale") or [])
    if stale:
        def _one(e: dict) -> str:
            # 사유가 있으면 사유를 쓴다 — 경과일수로 말할 수 없는 고장이 있다. 미국 시세는
            # 시장 마지막 봉이 최신이어도 개별 종목 428/503이 뒤처질 수 있고 그때 age는 0이라
            # 배너가 `미국 시세(0일)` 이 됐다(실측). 0일이라 적으면 고장이 아닌 것처럼 읽힌다.
            if e.get("stall_note"):
                return f"{e['label']} {e['stall_note']}"
            if e.get("age_hours") is not None:
                return f"{e['label']}({e['age_hours'] / 24:.0f}일)"
            return str(e["label"])
        bits.append("갱신 멈춤 " + " · ".join(_one(e) for e in stale[:_STALL_NAMES])
                    + (f" 외 {len(stale) - _STALL_NAMES}개" if len(stale) > _STALL_NAMES else ""))
    pit = stall.get("pit") or {}
    if pit.get("missing_n"):
        # 결측일을 이름으로 적는다 — "몇 건"만 적으면 어느 날이 빈지 몰라 조사가 안 된다.
        days = pit.get("missing") or []
        bits.append(f"PIT 스냅샷 결측 {pit['missing_n']}거래일(" + ", ".join(d[5:] for d in days[:4])
                    + (f" 외 {len(days) - 4}일" if len(days) > 4 else "") + ")")
    elif pit.get("reason"):
        bits.append(f"PIT {pit['reason']}")
    hd = stall.get("harness_days")
    if hd is not None and hd >= 7:
        bits.append(f"판별력 검사 {hd}일 경과")
    elif hd is None:
        bits.append("판별력 검사 이력 없음")
    if not bits:
        return None
    return "🔧 " + " / ".join(bits)


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
    selection: dict | None = None,
    exposure: float | None = None,
    exposure_reasons: list[str] | None = None,
    event_queue: dict | None = None,
    crowding: dict | None = None,
    stall: dict | None = None,
    us_signals: list[Any] | None = None,
    us_selection: dict | None = None,
    prev_us_buy_count: int | None = None,
) -> str:
    """아침 브리핑 본문. signals는 SignalResult 리스트(국내), threshold는 국면 반영 유효 문턱.

    selection이 분위 모드면 헤더를 '매수문턱' 대신 '매수권 상위 N종목 · 익스포저 M%'로 쓴다 —
    화면·봇·브리핑이 서로 다른 기준을 말하면 안 된다.
    app_url이 있으면 마지막에 「앱에서 보기」 링크를 붙인다 — 링크가 없으면 브리핑을 읽고
    끝나서 D7(재방문)에 구조적으로 기여하지 못한다.
    prev_buy_count가 있으면 매수 종목 수의 전일 대비 증감을 함께 적는다(매일 같은 문장 방지).

    us_signals를 주면 **미국 블록**을 같은 형식으로 덧붙인다(한 메시지 · 두 구역). 두 번 보내지
    않는 이유: 정지 배너·실측·면책이 한 번만 나오면 되고, 알림이 둘로 나뉘면 하나만 읽힌다.
    국면 라벨·익스포저는 코스피 기준이라 미국 블록에 쓰지 않는다 — 같은 값을 다른 시장에
    붙이면 그게 곧 틀린 문장이다. `None`이면 블록 자체를 생략한다(수집 전 = 빈 구역이 아니다).
    """
    d = date or datetime.date.today()
    ranked = (selection or {}).get("mode") == "rank"

    lines = [f"☀️ {_date_line(d)} 아침 브리핑", ""]
    # 정지 탐지는 **맨 위**. 데이터가 멈춘 채로 아래 숫자를 읽으면 낡은 값을 오늘 값으로 믿는다.
    sl = stall_line(stall)
    if sl:
        lines.append(sl)
        lines.append("")
    # 2026-08-06: `시장 ZONE` → `지금 시장`. 인사이트 탭의 `경기 사이클`과 **구분**하는 것이
    # 이 라벨의 목적이므로(다개월 사이클 vs 오늘의 코스피 상태) 그 대비는 유지한다.
    zone = f"지금 시장 {regime_label}" if regime_label else "지금 시장 판정 없음"
    head = (_selection_line(selection, exposure, exposure_reasons) if ranked
            else _threshold_line(threshold, base_threshold, bump_reasons))
    lines.append(f"{zone} · {head}")
    lines.append("")

    lines += _picks_lines(signals, selection=selection, threshold=threshold,
                          prev_buy_count=prev_buy_count)

    # 미국 블록 — **같은 함수로 그린다.** 두 시장을 따로 조립하면 한쪽만 고쳐져 갈라진다
    # (이 리포가 봇·화면 점수에서 이미 겪은 병이다). 국면·익스포저는 코스피 기준이라 안 쓴다.
    if us_signals is not None:
        lines.append("")
        lines.append("── 🇺🇸 미국 (어젯밤 종가 기준)")
        us_head = _selection_line(us_selection, None, None) if (us_selection or {}).get("mode") == "rank" else None
        if us_head:
            lines.append(us_head)
        lines += _picks_lines(us_signals, selection=us_selection, threshold=threshold,
                              prev_buy_count=prev_us_buy_count)

    eq = event_queue or {}
    if (eq.get("pending") or 0) > 0:
        lines.append("")
        lines.append(
            f"이벤트 후보 잔여 {eq.get('pending', 0)}건 — 자동 판정 대기"
        )
    if crowding and crowding.get("warn"):
        lines.append("")
        lines.append(f"⚠ 편중 {crowding.get('note')}")
    elif crowding and crowding.get("data_quality"):
        lines.append("")
        lines.append(f"ℹ 데이터 {crowding.get('note')}")

    lines += ["", _accuracy_line(accuracy)]
    if app_url:
        # 딥링크는 시그널 탭으로 — D7 계측 지점(GET /api/signals)과 같은 화면이어야 한다.
        lines += ["", f"앱에서 보기 → {app_url.rstrip('/')}/#signal"]
    lines.append(DISCLAIMER)
    return "\n".join(lines)
