"""미국은 섹터 중립화가 통째로 꺼져 있었다.

`sectors.SECTOR_OF` 는 **국내 6자리 코드만** 담고 있어 `sector_of("AAPL")` 이 늘 None이었다.
그래서 `_valuation_scores(sector_neutral=True)` 가 미국 503종목을 전부 `_none` 그룹으로 보내고
섹터 중립화를 건너뛰었다 — 이 모듈 docstring이 경고한 바로 그 상황이다
("반도체는 원래 고PER인데 유니버스 비교하면 항상 고평가로 찍힘").
"""

from __future__ import annotations

from signal_desk.reference import sectors
from signal_desk.signals import valuation as val


def test_hardcoded_map_has_no_us_tickers():
    """전제 확인 — 바뀌면 이 파일의 설명을 갱신할 것."""
    for t in ("AAPL", "MSFT", "NVDA", "GOOGL", "DAL"):
        assert sectors.sector_of(t) is None


def _uni(rows):
    return [{"ticker": t, "name": t, **({"sector": s} if s else {})} for t, s in rows]


def _fund(rows):
    return {t: {"per": per, "pbr": pbr} for t, per, pbr in rows}


def test_us_sectors_are_actually_used():
    """테크 5 + 은행 5. 은행이 훨씬 싸면, 유니버스 비교에서 테크는 전부 고평가로 찍힌다."""
    tech = [(f"T{i}", 30 + i, 8 + i) for i in range(5)]
    bank = [(f"B{i}", 6 + i, 0.8 + i * 0.05) for i in range(5)]
    fund = _fund(tech + bank)
    uni = _uni([(t, "정보기술") for t, _, _ in tech] + [(t, "금융") for t, _, _ in bank])

    flat = val._valuation_scores(val._eligible(fund), sector_neutral=True)  # 섹터 정보 없음
    neut = val.scores(uni, fund)

    assert min(flat[f"T{i}"] for i in range(5)) > 50, "섹터 정보 없으면 테크가 전부 고평가"
    # 섹터 내로 재면 테크 중 가장 싼 종목이 0분위가 된다
    assert neut["T0"] == 0.0
    assert neut["B0"] == 0.0
    assert neut["T4"] == 100.0


def test_it_removes_bias_rather_than_favouring_growth():
    """특혜가 아니다 — 섹터 안에서 비싼 종목은 오히려 나빠진다."""
    # 커뮤니케이션 4종목 중 대상 종목이 가장 비싸다
    rows = [("X", 26, 7.0), ("C1", 8, 1.0), ("C2", 9, 1.1), ("C3", 10, 1.2)]
    # 유니버스에는 훨씬 비싼 다른 섹터가 섞여 있다
    rows += [(f"E{i}", 200 + i, 50 + i) for i in range(6)]
    fund = _fund(rows)
    uni = _uni([("X", "커뮤니케이션"), ("C1", "커뮤니케이션"), ("C2", "커뮤니케이션"),
                ("C3", "커뮤니케이션")] + [(f"E{i}", "에너지") for i in range(6)])
    flat = val._valuation_scores(val._eligible(fund), sector_neutral=True)
    neut = val.scores(uni, fund)
    assert neut["X"] > flat["X"], "섹터 안에서 비싼 종목이 좋아지면 그건 편향 제거가 아니다"
    assert neut["X"] == 100.0


def test_domestic_scores_do_not_move():
    """국내 universe.json 행에는 `sector` 키가 없다(실측 0/200) → 폴백이 걸려 점수 불변.

    국내는 사전등록 대상이라 이게 중요하다 — 한 자리라도 움직이면 판정이 무효다.
    """
    kr = [("005930", 12.0, 1.3), ("000660", 9.0, 1.1), ("005380", 5.0, 0.5),
          ("000270", 4.5, 0.45), ("105560", 6.0, 0.4), ("055550", 6.5, 0.42)]
    fund = _fund(kr)
    uni_no_sector = [{"ticker": t, "name": t} for t, _, _ in kr]
    before = val._valuation_scores(val._eligible(fund), sector_neutral=True)
    after = val.scores(uni_no_sector, fund)
    assert before == after, "국내 점수가 움직였다 — 사전등록 판정이 무효가 된다"


def test_small_sectors_still_fall_back():
    """표본 4 미만 섹터는 유니버스 percentile 유지 — 3종목 안에서 매긴 분위는 정보가 아니다."""
    rows = [(f"S{i}", 10 + i, 1 + i) for i in range(3)] + [(f"B{i}", 50 + i, 9 + i) for i in range(6)]
    fund = _fund(rows)
    uni = _uni([(f"S{i}", "소형섹터") for i in range(3)] + [(f"B{i}", "금융") for i in range(6)])
    neut = val.scores(uni, fund)
    flat = val._valuation_scores(val._eligible(fund), sector_neutral=False)
    for i in range(3):
        assert neut[f"S{i}"] == flat[f"S{i}"]


def test_screener_stays_absolute():
    """스크리너는 '절대 저평가' UX라 섹터 중립화를 쓰지 않는다."""
    src = open(val.__file__, encoding="utf-8").read()
    assert "sc = _valuation_scores(eligible, sector_neutral=False)" in src


def test_sector_map_prefers_universe_then_falls_back():
    m = val.sector_map([{"ticker": "AAPL", "sector": "정보기술"}, {"ticker": "005930"}])
    assert m == {"AAPL": "정보기술"}, "섹터 없는 행이 들어가면 안 된다(폴백이 걸려야 한다)"
