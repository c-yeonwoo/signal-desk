"""아침 브리핑에 미국 구역을 붙인다 — 국내만 나가고 있었다(2026-08-16 요청).

한 메시지에 두 구역으로 낸다. 두 번 보내지 않는 이유: 정지 배너·실측·면책이 한 번만 나오면
되고, 알림이 둘로 나뉘면 하나만 읽힌다.

규약 두 가지가 검사 대상이다.

1. **두 시장이 같은 함수로 그려진다**(`_picks_lines`). 따로 조립하면 한쪽만 고쳐져 문구·기준이
   갈라지고 그 차이는 어느 화면에도 안 뜬다(봇과 화면이 서로 다른 입력으로 점수를 조립하던
   병과 같다).
2. **국면·익스포저는 미국 구역에 안 쓴다.** 코스피 기준 값이라 다른 시장에 붙이면 그게 곧
   틀린 문장이다.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field

from signal_desk import digest


@dataclass
class _Sig:
    ticker: str
    name: str
    score: float
    kind: str = "BUY"
    event_risk: bool = False
    decision: object | None = None
    factor_scores: dict = field(default_factory=dict)


def _kr():
    return [_Sig("005930", "삼성전자", 1.9), _Sig("000270", "기아", 1.5),
            _Sig("005380", "현대차", 0.2, kind="HOLD")]


def _us():
    return [_Sig("USB", "U.S. Bancorp", 2.16), _Sig("EIX", "Edison International", 2.72),
            _Sig("NVDA", "엔비디아", 0.1, kind="HOLD")]


def _build(**kw):
    base = dict(signals=_kr(), regime_label="조정", threshold=1.0, base_threshold=1.0)
    return digest.build_morning(**{**base, **kw})


def test_us_block_is_absent_when_not_supplied():
    """기존 동작을 안 바꾼다 — 미국을 안 넘기면 구역이 없다."""
    assert "🇺🇸" not in _build()


def test_us_block_lists_us_buys():
    text = _build(us_signals=_us())
    assert "🇺🇸 미국" in text
    assert "Edison International +2.72" in text and "U.S. Bancorp +2.16" in text


def test_empty_us_universe_omits_the_block_rather_than_showing_zero():
    """**빈 구역은 `매수 0`으로 읽힌다** — 수집 정지가 정상으로 보인다(0의 이유 규칙)."""
    assert "🇺🇸" not in _build(us_signals=None)
    # 반대로 종목은 있는데 매수가 없으면 그건 정상이고 말해야 한다
    quiet = _build(us_signals=[_Sig("NVDA", "엔비디아", 0.1, kind="HOLD")])
    assert "🇺🇸" in quiet and "매수 시그널 0" in quiet.split("🇺🇸")[1]


def test_regime_and_exposure_do_not_leak_into_the_us_block():
    """국면·익스포저는 코스피 기준이다 — 미국에 붙이면 틀린 문장이다."""
    text = _build(us_signals=_us(), regime_label="과열",
                  selection={"mode": "rank", "rank_slots": 6, "universe": 200,
                             "cutoff_score": 1.2},
                  us_selection={"mode": "rank", "rank_slots": 10, "universe": 503,
                                "cutoff_score": 2.0},
                  exposure=0.4, exposure_reasons=["국면 조정"])
    us_part = text.split("🇺🇸")[1]
    assert "지금 시장" not in us_part and "익스포저" not in us_part
    assert "상위 10종목/503" in us_part, "미국은 미국 selection을 써야 한다"
    assert "상위 6종목/200" in text.split("🇺🇸")[0]


def test_both_markets_render_through_one_function():
    """따로 조립하면 한쪽만 고쳐져 갈라진다 — 호출이 **두 번**이어야 한다."""
    src = inspect.getsource(digest.build_morning)
    assert src.count("_picks_lines(") == 2, "국내·미국이 같은 렌더러를 쓰지 않는다"


def test_previous_counts_are_tracked_per_market():
    """한 키를 나눠 쓰면 어제 대비 증감이 섞인다."""
    from signal_desk import api
    assert api._DIGEST_PREV_KEY != api._DIGEST_PREV_KEY_US
    text = _build(us_signals=_us(), prev_buy_count=5, prev_us_buy_count=1)
    kr_part, us_part = text.split("🇺🇸")
    assert "어제 5" in kr_part and "어제 1" in us_part


def test_disclaimer_and_accuracy_appear_once_not_per_market():
    """한 메시지로 보내는 이유가 이것이다 — 두 번 나오면 길어지고 안 읽힌다."""
    text = _build(us_signals=_us())
    assert text.count(digest.DISCLAIMER) == 1
