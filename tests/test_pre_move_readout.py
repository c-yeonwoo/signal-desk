"""`pre_run_up_pct` 가 PIT에 쌓이기만 하고 아무도 읽지 않았다(2026-08-16).

이 리포가 다섯 번 겪은 "수집 코드는 있는데 아무도 안 불렀다"가 **관측 지표에서 재발**했다 —
`store.snapshot_signals` 가 값을 쓰지만 `pre_move.summary` 는 호출자가 0이었고, 화면·라우트
어디에도 없었다. 쌓기만 하고 사람이 들여다봐야 아는 관측은 영원히 안 본다.

읽기 경로는 **오늘 스냅샷 요약이 아니라 채점**이어야 한다. "매수권이 시장보다 더 오른 뒤에
잡힌다"는 관측만으로는 그게 나쁜지 알 수 없다 — 실현 수익으로 갈라 봐야 한다.

핵심 규약 — **같은 날 안에서** 중위로 가른다. 전 기간 한 문턱으로 자르면 상승장 날짜가 통째로
'많이 오름' 쪽에 몰려 **국면 차이를 사전상승 효과로 착각**한다(pooled 상관이 팩터 IC를 속인
것과 같은 함정).
"""

from __future__ import annotations

import re
from pathlib import Path

from signal_desk.signals import pre_move

_HTML = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web" / "index.html"
_API = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "api.py"


def _series(start: str, n: int, ret: float):
    """`start` 부터 n봉. 진입(발동 다음 거래일)에서 청산까지 정확히 `ret` 이 되도록.

    앞 2봉만 100으로 두고 나머지를 올린다 — 진입 인덱스가 0이든 1이든 청산(+5)이 오른 구간에
    들어가므로 채점 규약(`accuracy.forward_returns`)의 진입 정의에 의존하지 않는다.
    """
    import datetime
    d = datetime.date.fromisoformat(start)
    dates = [(d + datetime.timedelta(days=i)).isoformat() for i in range(n)]
    closes = [100.0 if i < 2 else 100.0 * (1 + ret) for i in range(n)]
    return dates, closes


def _row(date, ticker, pre, kind="BUY"):
    return {"date": date, "ticker": ticker, "kind": kind, "pre_run_up_pct": pre}


def test_high_pre_run_up_is_scored_against_low():
    """관측이 아니라 **채점**이다 — 사전 상승이 큰 쪽 실현수익이 실제로 낮은지."""
    rows, closes = [], {}
    for i in range(10):
        hi, lo = f"H{i}", f"L{i}"
        rows += [_row("2026-07-09", hi, 20.0), _row("2026-07-09", lo, 1.0)]
        closes[hi] = _series("2026-07-09", 8, -0.05)     # 많이 오른 뒤 → −5%
        closes[lo] = _series("2026-07-09", 8, +0.05)     # 덜 오른 뒤  → +5%
    out = pre_move.score_from_pit(rows, closes, horizon=5)
    assert out["n_high"] == 10 and out["n_low"] == 10
    assert out["avg_high_pct"] < out["avg_low_pct"]
    assert out["delta_pct"] < 0 and out["significant"], out


def test_split_is_per_date_not_pooled():
    """**날짜를 섞으면 국면 차이가 사전상승 효과로 둔갑한다.**

    07-09는 전 종목 사전상승이 낮고(0·2) 07-16은 높다(30·32). 전 기간 중위로 자르면
    07-16 전체가 '많이 오름'이 되어 그 날의 시장 수익이 곧 결론이 된다. 날짜별로 가르면
    각 날 안에서 절반씩 갈려 그 오염이 사라진다.
    """
    rows, closes = [], {}
    for i in range(6):
        for date, (a, b), day_ret in (("2026-07-09", (0.0, 2.0), -0.10),
                                      ("2026-07-16", (30.0, 32.0), +0.10)):
            for pre, tag in ((a, "a"), (b, "b")):
                t = f"{date[-2:]}{tag}{i}"
                rows.append(_row(date, t, pre))
                closes[t] = _series(date, 8, day_ret)
    out = pre_move.score_from_pit(rows, closes, horizon=5)
    # 날짜별 분할이면 두 집합 모두 두 날짜를 절반씩 담아 평균차가 0에 가깝다.
    assert abs(out["delta_pct"]) < 1.0, f"날짜를 섞어 갈랐다: {out}"
    assert out["n_dates"] == 2


def test_missing_pre_run_up_is_counted_not_silently_dropped():
    """조용히 줄어든 표본은 줄어든 줄 모른다 — 몇 건을 뺐는지 밝힌다."""
    rows = [_row("2026-07-09", "A", None), _row("2026-07-09", "B", 5.0)]
    out = pre_move.score_from_pit(rows, {}, horizon=5)
    assert out["skipped"]["no_pre_run_up"] == 1
    assert out["skipped"]["no_price"] == 1


def test_only_buy_kinds_are_scored():
    """HOLD를 섞으면 매수 판단이 아니라 유니버스 전체를 재게 된다."""
    rows = [_row("2026-07-09", "A", 20.0, kind="HOLD"),
            _row("2026-07-09", "B", 1.0, kind="HOLD")]
    closes = {"A": _series("2026-07-09", 8, -0.05), "B": _series("2026-07-09", 8, 0.05)}
    out = pre_move.score_from_pit(rows, closes, horizon=5)
    assert out["n_high"] == 0 and out["n_low"] == 0


def test_thin_sample_blocks_the_verdict():
    """**표본이 적으면 판정하지 않는다.** 부호만 보고 게이트를 만들면 곡선 맞추기다."""
    rows = [_row("2026-07-09", "A", 20.0), _row("2026-07-09", "B", 1.0)]
    closes = {"A": _series("2026-07-09", 8, -0.20), "B": _series("2026-07-09", 8, 0.20)}
    out = pre_move.score_from_pit(rows, closes, horizon=5)
    assert not out["significant"] and out["blocked_reason"], out


def test_single_row_dates_cannot_form_a_median():
    """하루 1건이면 그 날의 중위가 자기 자신이라 분할이 무의미하다."""
    rows = [_row("2026-07-09", "A", 20.0), _row("2026-07-16", "B", 1.0)]
    closes = {"A": _series("2026-07-09", 8, -0.05), "B": _series("2026-07-16", 8, 0.05)}
    out = pre_move.score_from_pit(rows, closes, horizon=5)
    assert out["n_high"] == 0 and out["n_low"] == 0


def test_it_is_actually_reachable():
    """라우트를 만들고 화면에 안 붙이면 그 기능은 존재하지만 닿을 수 없다."""
    api = _API.read_text(encoding="utf-8")
    assert '@app.get("/api/pre-move")' in api
    assert "score_from_pit" in api, "라우트가 채점을 안 부른다"
    html = re.sub(r"^\s*//.*$", "", _HTML.read_text(encoding="utf-8"), flags=re.M)
    assert "/api/pre-move" in html, "화면에서 안 부른다 — 쌓기만 하고 안 보는 관측이 된다"
    assert "사전 상승" in html, "shadow 패널에 블록이 없다"
