from signal_desk.signals import narrative
from signal_desk.signals.engine import SignalResult


def test_explain_with_both_components():
    r = SignalResult(
        ticker="005930", name="삼성전자", score=1.5, kind="BUY", confidence=0.7,
        technical_score=2.0, fundamental_score=1.0, has_fundamental=True,
        reasons=["[기술] MACD 골든크로스", "[기본] ROE 15.0% — 우수"],
    )
    text = narrative.explain(r)
    assert "MACD 골든크로스" in text
    assert "ROE 15.0% — 우수" in text
    assert "매수" in text
    assert "+1.50" in text
    assert "0.70" in text


def test_explain_without_fundamental_data():
    r = SignalResult(
        ticker="005930", name="삼성전자", score=-1.5, kind="SELL", confidence=0.2,
        technical_score=-1.5, fundamental_score=0.0, has_fundamental=False,
        reasons=["[기술] RSI 75.0 — 과매수"],
    )
    text = narrative.explain(r)
    assert "재무데이터는 아직 없어" in text
    assert "매도" in text
    assert "낮은" in text


def test_explain_hold_no_reasons():
    r = SignalResult(
        ticker="X", name="X", score=0.0, kind="HOLD", confidence=0.0,
        technical_score=0.0, fundamental_score=0.0, has_fundamental=True, reasons=[],
    )
    text = narrative.explain(r)
    assert "관망" in text
    assert "뚜렷한 신호는 없는" in text


def test_explain_includes_extra_factor_tags_generically():
    r = SignalResult(
        ticker="005930", name="삼성전자", score=1.5, kind="BUY", confidence=0.7,
        technical_score=2.0, fundamental_score=0.0, has_fundamental=False,
        reasons=[
            "[기술] MACD 골든크로스",
            "[저평가] PER·PBR 상대순위 상위 10% — 저평가 구간",
            "[낙폭과대] 최근 10일 -20.0% 급락 + RSI 25.0 과매도 — 반등 기대",
        ],
    )
    text = narrative.explain(r)
    assert "PER·PBR 상대순위 상위 10%" in text
    assert "반등 기대" in text
