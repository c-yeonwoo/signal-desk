from signal_desk.signals import valuation


def test_screen_ranks_cheapest_first():
    universe = [
        {"ticker": "A", "name": "가"},
        {"ticker": "B", "name": "나"},
        {"ticker": "C", "name": "다"},
    ]
    fundamentals = {
        "A": {"per": 30.0, "pbr": 3.0, "roe": 10.0},
        "B": {"per": 5.0, "pbr": 0.5, "roe": 8.0},
        "C": {"per": 15.0, "pbr": 1.5, "roe": 12.0},
    }
    out = valuation.screen(universe, fundamentals)
    assert [r["ticker"] for r in out] == ["B", "C", "A"]
    assert out[0]["valuation_score"] < out[-1]["valuation_score"]


def test_screen_excludes_missing_per_or_pbr():
    universe = [{"ticker": "A", "name": "가"}, {"ticker": "B", "name": "나"}]
    fundamentals = {
        "A": {"per": 10.0, "pbr": 1.0},
        "B": {"pbr": 1.0},  # per 없음(적자 기업 등) -> 제외
    }
    out = valuation.screen(universe, fundamentals)
    assert [r["ticker"] for r in out] == ["A"]


def test_screen_empty_when_no_eligible():
    out = valuation.screen([], {"A": {"per": 10.0}})
    assert out == []


def test_percentile_rank_handles_ties():
    ranks = valuation._percentile_rank({"A": 10.0, "B": 10.0, "C": 20.0})
    assert ranks["A"] == ranks["B"]
    assert ranks["A"] < ranks["C"]


def _mk(per_pbr):
    return {t: {"per": p, "pbr": p / 10} for t, p in per_pbr.items()}


def test_scores_sector_neutral_vs_universe(monkeypatch):
    # 섹터X(저PER군) 4종목, 섹터Y(고PER군) 4종목
    fund = _mk({"X1": 10, "X2": 20, "X3": 30, "X4": 40, "Y1": 100, "Y2": 200, "Y3": 300, "Y4": 400})
    secmap = {"X1": "X", "X2": "X", "X3": "X", "X4": "X", "Y1": "Y", "Y2": "Y", "Y3": "Y", "Y4": "Y"}
    monkeypatch.setattr(valuation.sectors, "sector_of", lambda t: secmap.get(t))
    sc = valuation.scores([], fund)
    # 섹터 중립: Y1(섹터 내 최저 PER)은 절대 PER이 높아도 '섹터 내 저평가'로 낮은 점수
    assert sc["Y1"] == sc["X1"] == 0.0            # 각 섹터 내 최저 → 0
    assert sc["Y4"] == sc["X4"] == 100.0          # 각 섹터 내 최고 → 100
    # 유니버스 기준(screen)이라면 Y1은 고평가 상위였을 것 — 대비
    uni = valuation._valuation_scores(valuation._eligible(fund), sector_neutral=False)
    assert uni["Y1"] > sc["Y1"]                    # 유니버스로는 Y1이 훨씬 고평가


def test_scores_small_sector_falls_back_to_universe(monkeypatch):
    # 섹터 표본 < _MIN_SECTOR(4) → 유니버스 percentile로 fallback
    fund = _mk({"A": 10, "B": 20, "C": 30})       # 각자 다른 섹터 1종목씩
    monkeypatch.setattr(valuation.sectors, "sector_of", lambda t: t)   # 전부 단독 섹터
    sc = valuation.scores([], fund)
    uni = valuation._valuation_scores(valuation._eligible(fund), sector_neutral=False)
    assert sc == uni                               # 전부 fallback → 유니버스와 동일
