"""수급 팩터 — 외국인·기관 순매수 강도 컴포넌트 + 엔진 반영."""

from signal_desk.signals import engine, flow


def test_flow_buy_intensity():
    n, w, r, inten, has = flow.component({"intensity": 0.4, "foreign_net": 100, "inst_net": 50}, 0.2)
    assert has and w == 0.2 and n > 0 and inten == 0.4
    assert "[수급]" in r[0] and "순매수" in r[0] and "외국인" in r[0] and "기관" in r[0]


def test_flow_sell_intensity():
    n, w, r, inten, has = flow.component({"intensity": -0.3, "foreign_net": -100, "inst_net": -20}, 0.2)
    assert has and n < 0 and "순매도" in r[0]


def test_flow_none_and_weak_excluded():
    assert flow.component(None, 0.2) == (0.0, 0.0, [], 0.0, False)
    n, w, r, inten, has = flow.component({"intensity": 0.005, "foreign_net": 1, "inst_net": 0}, 0.2)
    assert not has and w == 0.0            # 미미한 수급은 노이즈 → 중립·제외


def test_evaluate_includes_flow():
    uni = [{"ticker": "005930", "name": "삼성전자"}]
    prices = {"005930": [100.0] * 60 + [101.0, 102.0, 103.0]}
    r = engine.evaluate(uni, prices, flows={"005930": {"intensity": 0.5, "foreign_net": 1, "inst_net": 1}})[0]
    assert r.has_flow and r.flow_intensity == 0.5
    assert any("[수급]" in x for x in r.reasons)
    # 수급 데이터 없으면 팩터 제외(그레이스풀)
    r2 = engine.evaluate(uni, prices)[0]
    assert r2.has_flow is False and r2.flow_intensity is None


def test_fetch_flows_circuit_breaker(monkeypatch, tmp_path):
    """수급 소스가 통째로 막히면(전부 None) 8연속 실패 후 조기 중단 — 도배 방지."""
    from signal_desk import store
    from signal_desk.ingest import naver
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(naver, "investor_flow", lambda t, days=20: calls.append(t) or None)
    uni = [{"ticker": f"{i:06d}", "name": f"n{i}"} for i in range(200)]
    out = store.fetch_flows(uni)
    assert out == {} and len(calls) == 8  # 8연속 실패 시 중단(200 전부 두드리지 않음)
    assert not store.FLOWS_FILE.exists()  # 빈 결과는 기존 flows.json 덮어쓰지 않음


def test_fetch_flows_from_naver(monkeypatch, tmp_path):
    """네이버 수급 → intensity 정규화(외국인+기관 순매수 / 거래량)."""
    from signal_desk import store
    from signal_desk.ingest import naver
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    monkeypatch.setattr(naver, "investor_flow",
                        lambda t, days=20: {"foreign_net": 3e6, "inst_net": 1e6, "total_buy": 20e6})
    out = store.fetch_flows([{"ticker": "005930", "name": "삼성전자"}])
    assert out["005930"]["intensity"] == 0.2  # (3+1)/20
    assert store.FLOWS_FILE.exists()
