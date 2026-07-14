"""시장 국면 체온계 — 조정심화 → 바닥다지기 → 회복초기 → 정상/강세 ZONE을 **감지**(예측 아님).

기존 regime.classify(스냅샷 라벨)와 달리 **궤적**을 본다: breadth의 수준+변화, 지수의 추세·낙폭·
변동성 추이, 그리고 '회복 신호 체크리스트'. 각 지표를 투명하게 노출하고, 바닥/반등을 **맞히지 않고**
지금이 어느 구간인지 확률적으로 읽는다(문헌: 복합지표는 정확한 시점이 아니라 다개월 ZONE을 식별).

assess(prices_by_ticker, index_closes, macro_result) → 순수 함수(입력 주입, 테스트 분리).
"""

from __future__ import annotations

from signal_desk.signals import indicators as ind

_MA = 20            # breadth 기준 이동평균
_LOOKBACK = 20      # 궤적 비교 창(약 1개월)
_DD_WINDOW = 120    # 낙폭 기준 고점 창(약 6개월)


def _breadth(prices_by_ticker: dict[str, list[float]]) -> tuple[float | None, float | None, int]:
    """(현재 %>MA20, 약 20거래일 전 %>MA20, 표본수). 종목별 자기 종가/MA만 써서 날짜 정렬 불필요."""
    now_above = prev_above = n = 0
    for closes in prices_by_ticker.values():
        if len(closes) < _MA + _LOOKBACK + 1:
            continue
        n += 1
        ma_now = ind.sma(closes, _MA)[-1]
        if ma_now is not None and closes[-1] > ma_now:
            now_above += 1
        prev = closes[: len(closes) - _LOOKBACK]
        ma_prev = ind.sma(prev, _MA)[-1]
        if ma_prev is not None and prev[-1] > ma_prev:
            prev_above += 1
    if n == 0:
        return None, None, 0
    return round(now_above / n * 100, 1), round(prev_above / n * 100, 1), n


def _stdev(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def _index_metrics(closes: list[float]) -> dict:
    """지수(동일가중 프록시) 추세·낙폭·모멘텀·변동성."""
    if not closes or len(closes) < 61:
        return {}
    ma20 = ind.sma(closes, 20)[-1]
    ma60 = ind.sma(closes, 60)[-1]
    peak = max(closes[-_DD_WINDOW:])
    drawdown = round((closes[-1] / peak - 1) * 100, 1) if peak else None
    mom20 = round((closes[-1] / closes[-21] - 1) * 100, 2)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    v20, v60 = _stdev(rets[-20:]), _stdev(rets[-60:])
    return {
        "above_ma20": ma20 is not None and closes[-1] > ma20,
        "above_ma60": ma60 is not None and closes[-1] > ma60,
        "drawdown_pct": drawdown, "mom20_pct": mom20,
        "vol20": round((v20 or 0) * 100, 2), "vol60": round((v60 or 0) * 100, 2),
        "vol_falling": (v20 is not None and v60 is not None and v20 < v60),
    }


def assess(prices_by_ticker: dict[str, list[float]], index_closes: list[float] | None = None,
           macro_result: dict | None = None) -> dict:
    """시장 국면 체온계 스냅샷: {zone, score, indicators[], recovery, summary, ready}."""
    b_now, b_prev, n = _breadth(prices_by_ticker or {})
    if b_now is None:
        return {"ready": False, "zone": None, "score": None, "indicators": [],
                "recovery": {"met": 0, "total": 0, "items": []}, "summary": "데이터 부족 — 국면 판정 보류"}
    im = _index_metrics(index_closes or [])
    b_chg = round(b_now - b_prev, 1)

    # 회복 신호 체크리스트(각 binary — '바닥/반등' 확정 아니라 '개선 징후')
    checks = [
        ("breadth_up", f"저변 개선(폭 {b_chg:+.1f}%p)", b_chg >= 5),
        ("reclaim_ma20", "지수 20일선 회복", bool(im.get("above_ma20"))),
        ("vol_calm", "변동성 진정(20일<60일)", bool(im.get("vol_falling"))),
        ("mom_up", "지수 20일 모멘텀 양(+)", (im.get("mom20_pct") or 0) > 0),
    ]
    met = sum(1 for *_, ok in checks if ok)
    total = len(checks)

    # ZONE 판정(궤적 기반, 투명 규칙)
    deteriorating = b_now < 40 and b_chg <= -3
    if b_now >= 55 and im.get("above_ma60"):
        zone = "강세" if b_now >= 70 else "정상"
    elif met >= 3:
        zone = "회복 초기"
    elif deteriorating:
        zone = "조정 심화"
    else:
        zone = "바닥 다지기"

    # 회복 진행도 점수(0~100, 확률 아님 — 대략적 복합 게이지)
    score = max(0, min(100, round(0.4 * b_now + 12 * met + (12 if im.get("above_ma60") else 0))))

    def st(ok, watch=False):
        return "ok" if ok else ("watch" if watch else "warn")

    indicators = [
        {"key": "breadth", "label": "시장 저변(20일선 위 비중)",
         "value": f"{b_now:.0f}% ({b_chg:+.1f}%p)", "read": "개선" if b_chg >= 5 else "악화" if b_chg <= -5 else "횡보",
         "state": st(b_now >= 50, watch=b_now >= 40)},
        {"key": "trend", "label": "지수 추세",
         "value": ("20·60일선 위" if im.get("above_ma60") else "20일선 위" if im.get("above_ma20") else "이동평균 아래"),
         "read": "상방" if im.get("above_ma60") else "회복 시도" if im.get("above_ma20") else "하방",
         "state": st(im.get("above_ma60"), watch=im.get("above_ma20"))},
        {"key": "drawdown", "label": "고점 대비 낙폭",
         "value": f"{im.get('drawdown_pct')}%" if im.get("drawdown_pct") is not None else "—",
         "read": "", "state": st((im.get("drawdown_pct") or 0) > -10, watch=(im.get("drawdown_pct") or 0) > -20)},
        {"key": "vol", "label": "변동성 추이(20일/60일)",
         "value": f"{im.get('vol20')}% / {im.get('vol60')}%" if im else "—",
         "read": "진정" if im.get("vol_falling") else "확대", "state": st(im.get("vol_falling"))},
    ]
    if macro_result and macro_result.get("bias"):
        indicators.append({"key": "macro", "label": "거시",
                           "value": str(macro_result.get("bias")), "read": "",
                           "state": "ok" if macro_result.get("bias") == "우호" else "warn"})

    _ZONE_MSG = {
        "강세": "저변 넓고 지수 상방 — 매수 신호가 나오는 우호 국면",
        "정상": "시장 저변 양호 — 정상 범위",
        "회복 초기": f"회복 징후 {met}/{total} 충족 — 조정 마무리·반등 시도 구간(확정 아님)",
        "바닥 다지기": "저변 낮으나 급격한 악화는 멈춘 횡보 — 바닥 확인 대기",
        "조정 심화": "저변 하락·지수 하방 — 조정 진행 중(떨어지는 칼 주의)",
    }
    summary = f"국면: {zone} · {_ZONE_MSG.get(zone, '')} · 회복 신호 {met}/{total}"
    return {"ready": True, "zone": zone, "score": score,
            "indicators": indicators,
            "recovery": {"met": met, "total": total,
                         "items": [{"label": lbl, "ok": ok} for _, lbl, ok in checks]},
            "breadth_pct": b_now, "n": n, "summary": summary}
