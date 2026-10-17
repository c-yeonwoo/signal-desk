"""Actual score contributions must not be confused with raw factor values."""

import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from signal_desk import api
from signal_desk.signals import engine, pick_reason


def test_zero_weight_raw_indicator_is_not_a_score_contribution():
    s = engine.evaluate([{"ticker": "005930", "name": "삼성전자"}],
                        {"005930": [100 - i for i in range(20)]},
                        today=dt.date(2026, 7, 1))[0]
    assert s.factor_scores["technical"] > 0
    assert s.score_contributions["technical"] == 0
    assert s.factor_suppressed["technical"] == "설정 가중치 0"
    assert round(sum(s.score_contributions.values()), 2) == s.score


def test_suppressed_value_is_retained_but_not_claimed_as_support(monkeypatch):
    monkeypatch.setattr(engine, "_valuation_component",
                        lambda *a: (0.8, 0.2, ["저평가"], 10, True))
    monkeypatch.setattr(engine, "_reversion_component",
                        lambda *a: (0.6, 0.15, ["낙폭"], 0.6, True))
    monkeypatch.setattr(engine, "_downtrend_blocking", lambda *a: True)
    s = engine.evaluate([{"ticker": "005380", "name": "현대차"}],
                        {"005380": [200 - i * .1 for i in range(300)]},
                        today=dt.date(2026, 7, 1))[0]
    for key in ("valuation", "reversion"):
        assert s.factor_scores[key] > 0
        assert s.score_contributions[key] == 0
        assert s.factor_suppressed[key] == "하락추세로 매수 기여 제외"
    assert round(sum(s.score_contributions.values()), 2) == s.score
    assert pick_reason.from_signal(s)["score_contributions"] == s.score_contributions
    row = api._list_row_from_signal(s, name=s.name, sector=None, price=100,
                                   change_pct=0, mktcap=1, vol=1, vol_avg=1, per=None, pbr=None)
    assert row["score_contributions"] == s.score_contributions
    assert row["factor_suppressed"] == s.factor_suppressed


@pytest.mark.parametrize("tail", [0.5, 0.8, 1, 1.3])
def test_attribution_preserves_combine_score_and_rank_inputs(monkeypatch, tail):
    calls = []
    combine = engine.combine
    def capture(components, config=None, **kwargs):
        result = combine(components, config, **kwargs)
        calls.append(result["score"])
        return result
    monkeypatch.setattr(engine, "combine", capture)
    prices = {f"{i:05d}0": [100 + j * (i + 1) / 100 for j in range(280)]
              + [200 * tail] * 20 for i in range(20)}
    results = engine.evaluate([{"ticker": t, "name": t} for t in prices], prices,
                              today=dt.date(2026, 7, 1))
    assert sorted(calls) == sorted(s.score for s in results)
    for s in results:
        assert round(sum(s.score_contributions.values()), 2) == s.score


def test_score_summary_uses_actual_contribution_not_raw_positive():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser summary execution")
    html = (Path(__file__).resolve().parents[1] / "src/signal_desk/web/index.html").read_text()
    func = "function scoreBeside(" + html.split("function scoreBeside(", 1)[1].split(
        "\nfunction sigPillTitle", 1)[0]
    script = "const _FACTOR_ORDER=['technical','momentum']; const _FACTOR_META={technical:{ko:'기술'},momentum:{ko:'모멘텀'}}; const _GATE_SHORT={};\n" + func
    script += "\nconsole.log(JSON.stringify([scoreBeside({factor_scores:{technical:1,momentum:.5},score_contributions:{technical:0,momentum:.01}}),scoreBeside({factor_scores:{technical:1}})]));"
    result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    current, legacy = json.loads(result.stdout)
    assert "모멘텀" in current and "기술" not in current
    assert "실제 기여 미확인" in legacy
