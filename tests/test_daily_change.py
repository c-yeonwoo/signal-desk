"""어제와 달라진 것 — 원인을 정직하게 가르는지 검사한다.

이 카드의 값어치는 전부 **인과가 맞는가**에 달려 있다. 틀린 원인을 매일 보여주면
그건 학습이 아니라 오학습이다.
"""

from __future__ import annotations

from signal_desk.signals import daily_change as dc


def _row(date, ticker, kind, score, **kw):
    r = {"date": date, "ticker": ticker, "kind": kind, "score": score}
    r.update(kw)
    return r


def test_score_drop_is_attributed_to_the_factors_that_moved():
    """점수가 내려가 강등됐으면 **어느 팩터가** 내려갔는지 함께 말한다."""
    rows = [_row("D1", "A", "STRONG_BUY", 1.86, technical=0.20, momentum=0.52),
            _row("D2", "A", "HOLD", 1.55, technical=-0.10, momentum=0.29)]
    out = dc.diff(rows)
    c = out["changes"][0]
    assert c["cause"]["kind"] == "factor"
    facs = {f["factor"] for f in c["cause"]["factors"]}
    assert {"technical", "momentum"} <= facs
    # 가장 크게 움직인 것이 앞에 온다 — 셋만 보여주므로 순서가 정보다.
    deltas = [abs(f["delta"]) for f in c["cause"]["factors"]]
    assert deltas == sorted(deltas, reverse=True)


def test_score_up_but_demoted_is_called_rank_not_factor():
    """**점수가 올랐는데 강등**된 경우를 '점수 때문'이라 말하면 거짓이다.

    실측 KCC: +1.81 → +2.04 인데 강등. 이게 "왜 3.0인데 관망이냐"는 혼란의 정체다.
    """
    rows = [_row("D1", "A", "STRONG_BUY", 1.81), _row("D2", "A", "HOLD", 2.04)]
    c = dc.diff(rows)["changes"][0]
    assert c["cause"]["kind"] == "rank", f"원인을 {c['cause']['kind']} 로 잘못 짚었다"
    assert "올랐는데도" in c["cause"]["text"]


def test_gate_and_coverage_win_over_score():
    """게이트·커버리지는 점수와 무관하게 자격을 바꾼다 — 점수 변화보다 먼저 말해야 한다."""
    gate = dc.diff([_row("D1", "A", "BUY", 1.5, gate_blocked=0),
                    _row("D2", "A", "HOLD", 1.2, gate_blocked=1)])["changes"][0]
    assert gate["cause"]["kind"] == "gate"
    cov = dc.diff([_row("D1", "A", "BUY", 1.5, low_coverage=0),
                   _row("D2", "A", "HOLD", 2.0, low_coverage=1)])["changes"][0]
    assert cov["cause"]["kind"] == "coverage"


def test_news_is_never_the_cause():
    """뉴스·공시를 원인으로 쓰면 "이 기사 때문에 관망"이라는 없는 인과가 만들어진다."""
    from pathlib import Path
    src = Path("src/signal_desk/signals/daily_change.py").read_text(encoding="utf-8")
    for banned in ("kb", "news", "sentiment", "disclosure"):
        assert banned not in src.lower().split('"""')[2], \
            f"원인 판정 코드가 {banned} 를 본다 — 맥락을 원인으로 쓰면 사후 합리화다"
    # 원인 종류는 넷뿐이다.
    kinds = {"factor", "rank", "gate", "coverage"}
    for k in kinds:
        assert f'"kind": "{k}"' in src
    api = Path("src/signal_desk/api.py").read_text(encoding="utf-8")
    blk = api.split("def daily_change_get(", 1)[1].split("\n@app.", 1)[0]
    assert '"layer": "맥락"' in blk, "공시를 맥락으로 라벨하지 않는다"


def test_quiet_day_says_so_instead_of_padding():
    """변화가 없으면 그 사실을 말한다 — 매일 장문이면 곧 안 읽힌다."""
    rows = [_row("D1", "A", "HOLD", 1.0), _row("D2", "A", "HOLD", 1.0)]
    out = dc.diff(rows)
    assert out["ready"] and out["quiet"] and not out["changes"]


def test_single_snapshot_says_why_not_just_empty():
    """스냅샷이 하루뿐인 것과 고장은 다르다 — 0의 이유를 말한다."""
    out = dc.diff([_row("D1", "A", "HOLD", 1.0)])
    assert out["ready"] is False
    assert "하루뿐" in out["blocked_reason"]


def test_payload_has_no_nan():
    """pandas 결손이 그대로 실리면 유효 JSON 이 아니다."""
    import json
    nan = float("nan")
    rows = [_row("D1", "A", "BUY", 1.5, technical=nan),
            _row("D2", "A", "HOLD", nan, technical=0.1)]
    json.dumps(dc.diff(rows), allow_nan=False)
