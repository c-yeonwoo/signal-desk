"""실측 정확도를 사전등록 대상으로 만든다(2026-08-17).

헤드라인 실측 지평이 20거래일인데 **그 지평의 매수 표본이 0**이다(매수권이 최근에야 생겨
h20 성숙 구간에 매수가 없었다 — 고장이 아니라 순서다). 반면 실제 매매는 단기다: 봇 채점 3일 ·
하네스 보유 5일. 그리고 h5에는 이미 매수 36건 · 리프트 +18.5%p가 쌓여 있었다.

**그 숫자는 근거가 될 수 없다** — 지평 3개(5·20·60) 중 좋아 보이는 하나를 고른 것이고,
판별력이 전혀 없어도 3번 보면 하나가 통과할 확률이 약 14%다. 그래서 아직 보지 않은 구간에
`from_date` 로 걸어 1회 확정한다.

통계는 소박한 이항 SE를 쓰지 않는다 — 같은 날 군집(시장 드리프트 공유)과 h일 창 중첩이
둘 다 SE를 **과소평가** 쪽으로 틀리게 한다. 겹치지 않는 h일 블록으로 묶어 블록 간 분산으로 잰다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from signal_desk import prereg
from signal_desk.signals import accuracy as acc

_HTML = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web" / "index.html"


def _look(**kw):
    req = {"from_date": "2026-08-18", "min_buy_sample": 10, "min_dates": 10,
           "min_lift_pp": 3.0, **kw.pop("requirement", {})}
    return {"id": "t", "horizon": 5, "requirement": req,
            "decision": {"if_pass": "P", "if_fail": "F"}, **kw}


def _hits(n_dates: int, rate: float, start_day: int = 1):
    return {f"2026-09-{start_day + i:02d}": [True] * round(rate * 10) + [False] * (10 - round(rate * 10))
            for i in range(n_dates)}


# ---------- 등록 파일 자체 ----------

def test_the_registered_file_parses_and_counts_the_new_look():
    reg = prereg.load()
    assert reg["ok"], reg["reason"]
    ids = [a["id"] for a in reg["accuracy_looks"]]
    assert "buy-lift-h5-oos" in ids
    assert reg["n_looks_total"] == (reg["n_canonical"] + len(reg["accuracy_looks"])
                                    + len(reg.get("ic_looks") or []))


def test_adding_it_raised_the_harness_threshold():
    """**종류를 나눠 n을 낮추면 그게 사후 완화다.** 정확도 look도 하네스 문턱을 올린다."""
    reg = prereg.load()
    assert reg["threshold_pct"] == prereg.sidak_threshold_pct(reg["n_looks_total"])
    assert reg["threshold_pct"] > prereg.sidak_threshold_pct(reg["n_canonical"]), \
        "정확도 look을 더했는데 하네스 문턱이 그대로다"


def test_registered_window_is_strictly_after_registration():
    """실측은 **이미 매일 화면에 보이는 값**이라 사후등록 위험이 하네스보다 크다."""
    lk = next(a for a in prereg.load()["accuracy_looks"] if a["id"] == "buy-lift-h5-oos")
    assert lk["requirement"]["from_date"] > lk["registered_at"]


def test_lift_threshold_matches_the_engine_constant():
    """두 곳이 갈라지면 화면과 판정이 다른 말을 한다."""
    lk = next(a for a in prereg.load()["accuracy_looks"] if a["id"] == "buy-lift-h5-oos")
    assert lk["requirement"]["min_lift_pp"] == pytest.approx(float(acc.MIN_LIFT_PP))


# ---------- 파싱 거부 규약 ----------

def test_bad_registrations_are_refused(tmp_path):
    """**파싱 단계에서** 막는다 — 나중에 판정에서 거르면 그 사이에 숫자가 화면에 뜬다."""
    base = (Path(__file__).resolve().parents[1] / "docs" / "preregistered.toml").read_text("utf-8")
    # 변이 문자열은 **파일에서 고유해야** 한다. `from_date`·`horizon` 은 accuracy_look과
    # ic_look 둘 다 갖고 있으므로 앞 줄까지 묶어 고유하게 만든다.
    cases = {
        "from_date 없음": ('min_buy_sample = 60', "# 지움"),
        "등록일 이하": ('from_date      = "2026-08-18"   # 등록 다음 날. 이 앞은 이미 본 구간이다\nmin_buy_sample',
                    'from_date      = "2026-08-17"\nmin_buy_sample'),
        "지평 0": ("horizon       = 5          # 봇 채점(3일)·하네스 보유(5일)와 같은 단기 쪽\nmarket        = \"kr\"\nhypothesis    = \"\"\"\n매수·강력매수",
                 "horizon       = 0\nmarket        = \"kr\"\nhypothesis    = \"\"\"\n매수·강력매수"),
        "리프트 문턱 0": ("min_lift_pp    = 3.0", "min_lift_pp    = 0.0"),
        "IC 요건 0": ("min_independent = 63", "min_independent = 0"),
        "mie 0": ("mie             = 0.08", "mie             = 0.0"),
    }
    for why, (old, new) in cases.items():
        f = tmp_path / f"{abs(hash(why))}.toml"
        assert base.count(old) == 1, (why, old)
        f.write_text(base.replace(old, new), encoding="utf-8")
        out = prereg.load(f)
        assert not out["ok"], f"{why}: 통과하면 안 된다"


# ---------- 판정 ----------

def test_pending_hides_the_number_entirely():
    """요건 미달 동안 리프트를 보이면 매일 보게 되고 그게 곧 다중검정이다."""
    v = prereg.judge_accuracy(_look(), hits_by_date=_hits(3, 0.9),
                              baseline_pct=50.0, baseline_sample=1000, n_looks=4)
    assert v["status"] == "pending" and v["verdict"] == "판정 보류"
    assert v["lift_pp"] is None and v["lift_lower_pp"] is None and v["precision_pct"] is None
    assert "3/10" in v["verdict_why"] or "30/10" in v["verdict_why"]


def test_strong_lift_passes_once_the_requirement_is_met():
    v = prereg.judge_accuracy(_look(), hits_by_date=_hits(20, 0.9),
                              baseline_pct=50.0, baseline_sample=4000, n_looks=4)
    assert v["status"] == "decided" and v["verdict"] == "판별력 있음"
    assert v["lift_lower_pp"] > 3.0 and v["decision"] == "P"


def test_point_estimate_above_the_bar_is_not_enough():
    """**하한**이 문턱을 넘어야 한다. 점추정만 보면 오차 안의 차이를 판별력이라 부르게 된다."""
    hits = {f"2026-09-{i:02d}": [True] * 6 + [False] * 4 if i % 2 else [True] * 4 + [False] * 6
            for i in range(1, 21)}
    v = prereg.judge_accuracy(_look(), hits_by_date=hits,
                              baseline_pct=45.0, baseline_sample=4000, n_looks=4)
    assert v["lift_pp"] > 3.0, "점추정은 문턱 위여야 검사가 의미 있다"
    assert v["verdict"] == "판별력 없음" and v["decision"] == "F"


def test_more_looks_make_it_harder():
    """Šidák — 데이터를 더 볼수록 문턱이 올라간다. 안 그러면 종류를 늘려 통과를 산다."""
    kw = dict(hits_by_date=_hits(20, 0.72), baseline_pct=50.0, baseline_sample=4000)
    lo = prereg.judge_accuracy(_look(), n_looks=1, **kw)
    hi = prereg.judge_accuracy(_look(), n_looks=12, **kw)
    assert hi["threshold_z"] > lo["threshold_z"]
    assert hi["lift_lower_pp"] < lo["lift_lower_pp"]


def test_oos_window_actually_cuts_the_rows():
    """`from_date` 가 문구가 아니라 **데이터를 자른다** — 안 자르면 이미 본 구간이 섞인다."""
    rows = [{"date": "2026-08-10", "ticker": "A", "kind": "BUY"},
            {"date": "2026-08-20", "ticker": "A", "kind": "BUY"}]
    dates = [f"2026-08-{d:02d}" for d in range(10, 31)]
    closes = {"A": (dates, [100.0 if i < 2 else 130.0 for i in range(len(dates))])}
    everything = acc.buy_hits_by_date(rows, closes, horizon=5)
    oos = acc.buy_hits_by_date(rows, closes, horizon=5, from_date="2026-08-18")
    assert "2026-08-10" in everything and "2026-08-10" not in oos


# ---------- 통계 규약 ----------

def test_clustered_days_do_not_inflate_confidence():
    """같은 날 20종목은 관측 20개가 아니다 — 블록으로 묶어야 SE가 정직하다."""
    one_day = {"2026-09-01": [True] * 20}
    v = acc.block_lift_verdict(one_day, baseline_pct=50.0, baseline_sample=4000,
                               horizon=5, z=2.5, min_lift_pp=3.0)
    assert not v["passes"] and v["n_blocks"] == 1
    assert "블록" in (v["blocked_reason"] or "")


def test_blocks_are_non_overlapping_windows():
    """h일씩 겹치는 창을 독립으로 세면 SE가 과소평가된다."""
    hits = _hits(20, 0.8)
    v = acc.block_lift_verdict(hits, baseline_pct=50.0, baseline_sample=4000,
                               horizon=5, z=2.0, min_lift_pp=3.0)
    assert v["n_blocks"] == 4, f"20거래일 / 5일 블록 = 4개여야 한다: {v}"
    assert v["n"] == 200


def test_baseline_error_is_included():
    """기준선 표본이 작으면 SE가 커져야 한다 — 빼면 관대한 쪽으로 틀린다."""
    hits = _hits(20, 0.8)
    big = acc.block_lift_verdict(hits, baseline_pct=50.0, baseline_sample=100000,
                                 horizon=5, z=2.0, min_lift_pp=3.0)
    small = acc.block_lift_verdict(hits, baseline_pct=50.0, baseline_sample=30,
                                   horizon=5, z=2.0, min_lift_pp=3.0)
    assert small["se_pp"] > big["se_pp"]


# ---------- 닿을 수 있는가 ----------

def test_it_is_reachable_and_is_not_the_headline():
    """만들고 안 붙이면 닿을 수 없고, 첫 화면에 두면 판정 경로가 둘이 된다."""
    api = (Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "api.py"
           ).read_text(encoding="utf-8")
    assert '@app.get("/api/accuracy-verdict")' in api
    html = re.sub(r"^\s*//.*$", "", _HTML.read_text(encoding="utf-8"), flags=re.M)
    assert "/api/accuracy-verdict" in html, "화면에서 안 부른다"
    # 첫 화면 신뢰 스트립(verdictRow)이 이 라우트를 읽으면 안 된다 — 헤드라인은 하나다.
    i = html.find("function verdictRow")
    if i >= 0:
        assert "/api/accuracy-verdict" not in html[i:i + 2500], \
            "첫 화면이 두 번째 판정을 읽는다 — 갈리면 관대한 쪽이 읽힌다"
