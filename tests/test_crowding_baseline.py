"""섹터 편중을 절대 비율로 판정하고 있었다 — 기준선 없는 비율(2026-08-16).

`assess` 가 `top_pct >= 40%` 만 보고 `crowded` 를 판정했다. 유니버스 자체의 섹터 분포를
안 보므로 두 방향으로 틀린다.

실측(미국 매수권 10종목):

    금융     매수권 40%  vs 유니버스 15%  → 리프트 +25%p  (진짜 쏠림)
    소재     매수권 20%  vs 유니버스  5%  → 리프트 +15%p
    산업재   매수권 10%  vs 유니버스 17%  → 리프트  −7%p  (오히려 **덜** 뽑혔다)

그리고 표본이 작으면 우연이 경고로 읽힌다 — 국내 매수권은 4종목이라 2개만 같은 섹터여도
50%다. 판정은 **초기하 p-value**로 한다: "무작위로 n개 뽑아 이 섹터가 k개 이상 나올 확률".

이 리포의 1번 지표 규칙이 그대로 적용된다 — *base rate 없는 비율은 노출 금지. 기준선과
리프트를 항상 함께 내고, 판정도 절대값이 아니라 리프트로 한다.*
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from signal_desk.signals import crowding


@dataclass
class _Row:
    ticker: str
    kind: str
    sector: str

    def get(self, k, default=None):        # dict 경로도 같은 코드를 타는지 확인용
        return getattr(self, k, default)


def _universe(spec: dict[str, tuple[int, int]]) -> list[dict]:
    """{섹터: (전체 수, 매수권 수)} → 행 리스트."""
    rows, i = [], 0
    for sec, (total, buys) in spec.items():
        for j in range(total):
            i += 1
            rows.append({"ticker": f"T{i}", "sector": sec,
                         "kind": "BUY" if j < buys else "HOLD"})
    return rows


def test_hypergeom_matches_the_definition():
    """직접 센 값과 같아야 한다 — 통계 유틸을 새로 만들면 정의부터 박는다."""
    N, K, n = 20, 5, 4
    exact = sum(math.comb(K, i) * math.comb(N - K, n - i) for i in range(2, 5)) / math.comb(N, n)
    assert crowding.hypergeom_sf(2, N, K, n) == pytest.approx(exact)
    assert crowding.hypergeom_sf(0, N, K, n) == pytest.approx(1.0)
    assert crowding.hypergeom_sf(K + 1, N, K, n) == 0.0


def test_universe_heavy_sector_is_not_crowded():
    """**정상인데 경고**하던 경우. 유니버스의 절반이 정보기술이면 매수권 절반도 정상이다."""
    rows = _universe({"정보기술": (100, 5), "금융": (100, 5)})
    r = crowding.assess(rows)
    assert r["base_pct"] == 50.0 and r["lift_pp"] == pytest.approx(0.0)
    assert not r["warn"], f"기준선과 같은데 경고했다: {r['note']}"
    assert "유니버스 50.0%" in r["note"], "기준선을 안 보여주면 절대 비율과 구분되지 않는다"


def test_real_concentration_still_warns():
    """실측 미국 금융 4/10(유니버스 76/501) — 리프트 +25%p, p=0.05."""
    rows = _universe({"금융": (76, 4), "산업재": (83, 1), "정보기술": (73, 0),
                      "헬스케어": (59, 0), "유틸리티": (31, 1), "에너지": (21, 1),
                      "소재": (25, 2), "기타": (133, 1)})
    r = crowding.assess(rows)
    assert r["top_sector"] == "금융" and r["top_pct"] == 40.0
    assert r["base_pct"] == pytest.approx(15.2, abs=0.3)
    assert r["lift_pp"] > 20 and r["p_value"] <= 0.05
    assert r["warn"] and "crowded" in r["note"]


def test_small_sample_is_not_called_crowded_on_percentage_alone():
    """매수권 3종목 중 2개면 67%지만, 흔한 섹터라면 무작위로도 자주 나온다."""
    rows = _universe({"흔함": (120, 2), "나머지": (80, 1)})
    r = crowding.assess(rows)
    assert r["top_pct"] > 40, "예전 절대 문턱이라면 경고였을 상황이어야 검사가 의미 있다"
    assert not r["warn"], f"우연을 편중이라 불렀다: {r['note']}"


def test_lift_can_be_negative_and_is_reported():
    """`10% 차지`가 실제로는 **덜 뽑힌** 것일 수 있다 — 부호를 보여줘야 안다."""
    rows = _universe({"산업재": (83, 1), "금융": (76, 4), "기타": (342, 5)})
    r = crowding.assess(rows)
    assert r["baseline"]["산업재"] > (r["distribution"]["산업재"] / r["n_buy"] * 100)


def test_no_universe_means_no_verdict_not_a_confident_one():
    """매수권만 넘어오면 기준선이 없다 — **모른다고 말한다**. 조용히 절대 판정하지 않는다."""
    rows = [{"ticker": "A", "kind": "BUY", "sector": "금융"},
            {"ticker": "B", "kind": "BUY", "sector": "금융"},
            {"ticker": "C", "kind": "BUY", "sector": "소재"}]
    r = crowding.assess(rows)
    assert r["base_pct"] is None and r["p_value"] is None
    assert "판정 보류" in r["note"] or r["data_quality"]


def test_unmapped_pileup_is_still_a_data_gap_not_crowding():
    """섹터맵 공백은 crowded trade가 아니다 — 기존 규약을 유지한다."""
    rows = [{"ticker": f"T{i}", "kind": "BUY"} for i in range(5)]
    rows += [{"ticker": f"U{i}", "kind": "HOLD"} for i in range(50)]
    r = crowding.assess(rows)
    assert r["data_quality"] and not r["warn"]
    assert "섹터맵 공백" in r["note"]


def test_no_buys_reports_zero_without_inventing_a_verdict():
    rows = [{"ticker": f"T{i}", "kind": "HOLD", "sector": "금융"} for i in range(10)]
    r = crowding.assess(rows)
    assert r["n_buy"] == 0 and not r["warn"] and r["note"] == "매수권 없음"


def test_screen_reuses_the_server_sentence():
    """화면이 문장을 다시 만들면 브리핑과 갈라진다 — `note`를 그대로 쓴다."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web" / "index.html"
            ).read_text(encoding="utf-8")
    i = html.index("crowd.warn || crowd.data_quality) {")
    block = html[i:i + 600]
    assert "crowd.note" in block
    assert "buyN > 0" in block, "경고가 아닐 때 섹터 구성이 아예 안 보인다"
