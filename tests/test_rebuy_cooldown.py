"""방금 판 종목을 다시 사지 않는다 — 쿨다운이 로테이션 경로에만 있었다(2026-08-22).

## 증상 (프로덕션 실측)

    08-21 13:31  sell LyondellBasell @66.06 TRAILING
    08-21 13:31  buy  LyondellBasell @66.06 SIGNAL     ← 같은 분. 안정형 쿨다운은 7일이다

트레일링·손절은 **제대로 작동한다.** 문제는 팔자마자 점수가 그대로라 다시 사는 것이다 —
손실을 확정하고 왕복 비용까지 물면서 같은 자리로 돌아간다.

## 원인

`strategy.ROTATION_PRESETS[*]["cooldown_days"]`(3·5·7일)가 이미 있었고 주석에도
"방금 판 종목 재매수 금지(핑퐁 방지)"라고 적혀 있었는데, `_conviction_rotate` **한 곳에만**
걸려 있었다. `run_once` 의 일반 매수 경로에는 없었다.

이 리포가 반복해서 겪은 **"게이트가 한 경로에만 걸려 있다"** 의 재발이다(봇 국내 경로가
`evaluate` 에 `shorts` 를 안 넘겨 화면과 봇이 갈라진 것과 같은 병).

## 실측 영향

로컬 장부만으로 쿨다운이 있었다면 막혔을 재매수 **21건**(안정 1 · 균형 17 · 공격 3).
그중 4건은 **0.0시간**(같은 분) 재매수였다.
"""

from __future__ import annotations

import datetime
import inspect
import re

import pytest

from signal_desk import bot, strategy


@pytest.fixture()
def wired(monkeypatch):
    """`bot_trades_recent` 만 갈아끼운다 — 쿨다운 계산 자체를 재려면 DB는 필요 없다."""
    now = int(datetime.datetime.now(bot._KST).timestamp())
    trades = {"rows": []}
    monkeypatch.setattr(bot.db, "bot_trades_recent",
                        lambda uid, n, mkt: list(trades["rows"]))
    return now, trades


def test_recently_sold_is_excluded(wired):
    """**이게 그 버그다.** 5일 쿨다운인데 방금 판 종목이 후보에 남아 있었다."""
    now, trades = wired
    trades["rows"] = [{"ticker": "A", "side": "sell", "ts": now - 60},          # 1분 전
                      {"ticker": "B", "side": "sell", "ts": now - 6 * 86400}]   # 6일 전
    cooled = bot.recent_sold_tickers(1, "kr", "balanced")                        # 5일
    assert "A" in cooled, "1분 전에 판 종목이 후보에 남았다"
    assert "B" not in cooled, "쿨다운이 지난 종목까지 막으면 영구 차단이다"


def test_buys_do_not_start_a_cooldown(wired):
    """매수는 쿨다운 대상이 아니다 — 분할추가(ADD)가 자기 자신을 막으면 안 된다."""
    now, trades = wired
    trades["rows"] = [{"ticker": "A", "side": "buy", "ts": now - 60}]
    assert bot.recent_sold_tickers(1, "kr", "balanced") == set()


def test_cooldown_length_follows_the_style(wired):
    """성향별로 3·5·7일이다 — 한 값으로 뭉개면 성향 설정이 거짓말이 된다."""
    now, trades = wired
    trades["rows"] = [{"ticker": "A", "side": "sell", "ts": now - 4 * 86400}]   # 4일 전
    assert "A" in bot.recent_sold_tickers(1, "kr", "conservative")   # 7일 → 아직 막힘
    assert "A" in bot.recent_sold_tickers(1, "kr", "balanced")       # 5일 → 아직 막힘
    assert "A" not in bot.recent_sold_tickers(1, "kr", "aggressive")  # 3일 → 풀림


def test_zero_cooldown_blocks_nothing(wired):
    """0이면 아무 것도 막지 않아야 한다 — 끄는 경로가 막는 쪽으로 틀리면 안 된다."""
    now, trades = wired
    trades["rows"] = [{"ticker": "A", "side": "sell", "ts": now}]
    monkey = dict(strategy.rotation_params("balanced"))
    monkey["cooldown_days"] = 0
    import unittest.mock as m
    with m.patch.object(strategy, "rotation_params", lambda st: monkey):
        assert bot.recent_sold_tickers(1, "kr", "balanced") == set()


# ---------- 갈라짐 방지 ----------

def test_every_buy_candidate_filter_applies_the_cooldown():
    """**후보를 고르는 모든 자리**가 쿨다운을 봐야 한다. 한 곳만 빠지면 그 경로로 새어나간다."""
    src = inspect.getsource(bot)
    src = re.sub(r"^\s*#.*$", "", src, flags=re.M)          # 주석 제외(설명이 오탐)
    picks = [m.start() for m in re.finditer(r"engine\.is_buy\(s\.kind\)", src)]
    assert len(picks) >= 3, f"매수 후보 선별 자리를 {len(picks)}곳만 찾았다 — 패턴이 바뀌었다"
    for i in picks:
        blk = src[i:i + 500]
        # 후보 목록을 만드는 자리(=필터 컴프리헨션)만 대상. 단순 카운트는 제외한다.
        if "sum(" in src[max(0, i - 40):i]:
            continue
        assert "not in cooled" in blk or "not in recent_sold" in blk, (
            f"쿨다운 없는 매수 후보 선별이 있다:\n    {src[i-120:i+220]}")


def test_cooldown_is_computed_in_exactly_one_place():
    """두 곳에서 조립하면 한쪽만 고쳐진다 — 실제로 그래서 이 버그가 났다."""
    src = re.sub(r"^\s*#.*$", "", inspect.getsource(bot), flags=re.M)
    assert src.count("def recent_sold_tickers(") == 1
    # 원시 계산(`cooldown_days` 를 직접 곱하는 것)은 헬퍼 안에만 있어야 한다
    raw = [m.start() for m in re.finditer(r'cooldown_days"\]', src)]
    helper_at = src.index("def recent_sold_tickers(")
    helper_end = src.index("\ndef ", helper_at + 10)
    for i in raw:
        assert helper_at < i < helper_end, "쿨다운을 헬퍼 밖에서 직접 계산한다"


def test_the_preset_comment_promised_this():
    """설정에 값이 있고 주석이 약속했는데 코드가 안 지키면, 그게 가장 안 보이는 고장이다."""
    src = inspect.getsource(strategy)
    assert "cooldown_days" in src and "재매수 금지" in src
    for style in ("conservative", "balanced", "aggressive"):
        assert strategy.rotation_params(style)["cooldown_days"] > 0
