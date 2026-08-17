"""검정 통계량을 포트폴리오 수익률에서 횡단면 IC로 바꾼다 + 주간 트랙(2026-08-17).

## 왜 `min_periods` 조정으로는 안 되나 (실측)

같은 데이터로 두 통계량을 재봤다(대조군 300회):

    포트폴리오 기간수익  +0.253%/기간 · sd 4.19%p  → t 0.89 (219기간)
    횡단면 IC           +0.0200      · sd 0.1856  → t 1.56 (독립 210관측)

포트폴리오 수익률로는 연 20%p 초과수익을 잡는 데 **24년**, 연 5%p면 389년이 걸린다.
`min_periods` 를 30에서 어떤 값으로 바꿔도 안 잡힌다 — **문턱 문제가 아니라 통계량 문제**였고,
200종목 중 6개만 보느라 정보의 97%를 버리고 있었다.

IC는 1.75배 효율(= 같은 t에 3배 적은 표본)이고 데이터는 이미 있으므로 공짜다. 그래도 둘 다
유의하지 않다 — 통계량을 바꿔도 "지금은 모른다"는 결론은 안 바뀌고, 바뀌는 건 대기 기간뿐이다.

## 노이즈의 성질

IC 날짜별 sd 0.186 중 200종목 유한표본 몫은 1/√199 = 0.071 뿐이다. 나머지 0.172는 국면에
따라 팩터가 먹기도 안 먹기도 하는 **실제 변동**이라 종목을 더 넣어도 안 줄어든다.

## 판정 ≠ 진척

판정은 사전등록 요건을 채운 날 **1회·엄격**, 주간 트랙은 **상시·구속력 없음**이다. 진척을
근거로 파라미터를 바꾸면 그게 곧 다중검정이므로 `binding=False` 를 실어 보낸다.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from signal_desk import bot, prereg
from signal_desk.signals import accuracy as acc

_HTML = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web" / "index.html"
_API = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "api.py"
Z = 2.569        # look 5개일 때의 Šidák z


def _ics(n_dates: int, mean: float, sd: float = 0.1856, seed: int = 1):
    r = random.Random(seed)
    return {f"d{i:05d}": r.gauss(mean, sd) for i in range(n_dates)}


# ---------- 등록 ----------

def test_the_ic_look_is_registered_and_counted():
    reg = prereg.load()
    assert reg["ok"], reg["reason"]
    ids = [x["id"] for x in reg["ic_looks"]]
    assert "kr-score-ic-h5-oos" in ids
    assert reg["n_looks_total"] == (reg["n_canonical"] + len(reg["accuracy_looks"])
                                    + len(reg["ic_looks"]))


def test_requirement_matches_the_power_calculation():
    """요건은 **역산된 값**이어야 한다 — 둥근 숫자를 고르면 `min_periods=30` 과 같은 병이다."""
    lk = next(x for x in prereg.load()["ic_looks"] if x["id"] == "kr-score-ic-h5-oos")
    req = lk["requirement"]
    sd, mie = 0.1856, float(req["mie"])
    z = prereg.accuracy_z(prereg.load()["n_looks_total"])
    need = ((z + 0.84) * sd / mie) ** 2                 # 검정력 80%
    assert abs(req["min_independent"] - need) <= 3, (
        f"요건 {req['min_independent']} vs 역산 {need:.0f} — 검정력 계산과 어긋난다")


def test_it_raised_the_threshold_for_everything_else():
    """종류를 나눠 n을 낮추면 그게 사후 완화다."""
    reg = prereg.load()
    assert reg["threshold_pct"] == prereg.sidak_threshold_pct(reg["n_looks_total"])
    assert reg["threshold_pct"] > prereg.sidak_threshold_pct(reg["n_canonical"])


# ---------- 판정 ----------

def test_mie_sets_sample_size_not_the_decision_threshold():
    """**`mie` 를 문턱으로 쓰면 작지만 진짜인 우위를 기각한다.** 판정은 언제나 "0보다 큰가"다."""
    ics = _ics(63 * 5, mean=0.05)                        # mie(0.08)보다 작지만 진짜인 우위
    v = acc.ic_verdict(ics, horizon=5, z=Z, min_independent=63, mie=0.08)
    assert v["status"] == "decided" and v["verdict"] == "판별력 있음", v
    assert v["ic_lower"] > 0 and v["ic"] < 0.08


def test_zero_edge_is_rejected():
    v = acc.ic_verdict(_ics(63 * 5, mean=0.0, seed=3), horizon=5, z=Z,
                       min_independent=63, mie=0.08)
    assert v["verdict"] == "판별력 없음"


def test_overlapping_windows_are_discounted():
    """h일 창이 겹치면 독립 관측은 `날짜/h` 다 — 안 나누면 t가 부풀려진다."""
    ics = _ics(300, mean=0.05)
    v = acc.ic_verdict(ics, horizon=5, z=Z, min_independent=60, mie=0.08)
    naive = acc.ic_verdict(ics, horizon=1, z=Z, min_independent=60, mie=0.08)
    assert v["requirement"]["independent"] == pytest.approx(60.0)
    assert abs(naive["t"]) > abs(v["t"]), "중첩 보정이 t를 줄이지 않는다"


def test_pending_hides_the_number():
    """요건 미달 동안 IC가 보이면 매주 보게 되고 그게 곧 다중검정이다."""
    v = acc.ic_verdict(_ics(50, mean=0.2), horizon=5, z=Z, min_independent=63, mie=0.08)
    assert v["status"] == "pending"
    assert v["ic"] is None and v["t"] is None and v["ic_lower"] is None


# ---------- 주간 트랙 ----------

def test_progress_shows_t_while_the_verdict_stays_blank():
    """진척과 판정의 **차이가 규약이다** — 판정은 비우고 진척은 보여준다."""
    ics = _ics(50, mean=0.05)
    kw = dict(horizon=5, z=Z, min_independent=63, mie=0.08)
    assert acc.ic_verdict(ics, **kw)["t"] is None
    p = acc.ic_progress(ics, **kw)
    assert p["t"] is not None and p["binding"] is False


def test_futility_fires_when_waiting_is_pointless_but_not_early():
    """**조기 기각은 늦게 안전하게, 그러나 헛된 대기는 자른다.**

    우위가 전혀 없을 때 실측 시뮬레이션에서 독립 40관측(200거래일 ≈ 9.5개월)에 걸린다 —
    요건 63(15개월)을 다 기다릴 필요가 없다. 반대로 초반(20관측)에 걸리면 아직 알 수 없는
    것을 기각하는 것이다.
    """
    kw = dict(horizon=5, z=Z, min_independent=63, mie=0.08)
    early = acc.ic_progress(_ics(20 * 5, mean=0.0, seed=7), **kw)
    late = acc.ic_progress(_ics(50 * 5, mean=0.0, seed=7), **kw)
    assert not early["futile"], "초반에 기각하면 아직 모르는 것을 기각하는 것이다"
    assert late["futile"] and late["futility_reason"]


def test_futility_does_not_fire_on_a_real_edge():
    """**양성 대조군** — 진짜 우위를 조기 기각하면 검사가 해롭다."""
    p = acc.ic_progress(_ics(50 * 5, mean=0.10, seed=11), horizon=5, z=Z,
                        min_independent=63, mie=0.08)
    assert not p["futile"], p


# ---------- 손해 경보 ----------

def test_harm_alert_fires_on_confirmed_underperformance():
    """"좋다"보다 "나쁘다"가 빨리 결론난다 — 실계좌로 따라 사는 쪽의 실무 질문이다."""
    curve = [{"total_eval": 10_000_000 * (1 - 0.004 * i)} for i in range(30)]
    h = bot.harm_alert(curve, seed=10_000_000, benchmark_pct=7.0)
    assert h["ready"] and h["alert"] and h["upper_pp"] < 0


def test_harm_alert_stays_quiet_when_it_is_ahead():
    curve = [{"total_eval": 10_000_000 * (1 + 0.006 * i)} for i in range(30)]
    h = bot.harm_alert(curve, seed=10_000_000, benchmark_pct=3.0)
    assert h["ready"] and not h["alert"]


def test_harm_alert_needs_a_baseline_and_says_so():
    """기준선 없는 수익률은 판정이 아니다 — 없으면 **이유를 말한다**."""
    curve = [{"total_eval": 10_000_000} for _ in range(30)]
    h = bot.harm_alert(curve, seed=10_000_000, benchmark_pct=None)
    assert not h["ready"] and "벤치마크" in h["reason"]


def test_harm_alert_needs_enough_blocks():
    """일별 자산곡선은 자기상관이 강해 독립 관측이 아니다 — 블록이 모자라면 판정하지 않는다."""
    h = bot.harm_alert([{"total_eval": 1e7} for _ in range(6)], seed=1e7, benchmark_pct=1.0)
    assert not h["ready"] and "블록" in h["reason"]


# ---------- 닿을 수 있는가 ----------

def test_it_is_reachable_and_labeled_non_binding():
    api = _API.read_text(encoding="utf-8")
    assert '@app.get("/api/weekly-track")' in api
    html = re.sub(r"^\s*//.*$", "", _HTML.read_text(encoding="utf-8"), flags=re.M)
    assert "/api/weekly-track" in html, "화면에서 안 부른다"
    assert "판정 아님" in html, "진척을 판정처럼 보이게 두면 안 된다"
    i = html.find("function verdictRow")
    if i >= 0:
        assert "/api/weekly-track" not in html[i:i + 2500], \
            "첫 화면이 주간 진척을 읽는다 — 판정 경로가 둘이 된다"
