"""기후 시그널 — 기존 combine/kind 격리 · emphasized 갈래만."""

from signal_desk.signals import climate, engine as eng


def _mini_tree(*, affinity="risk_on", emp_edge="path", emp_sectors=None, alt_sectors=None):
    emp_sectors = emp_sectors or ["semiconductor"]
    alt_sectors = alt_sectors or ["defense"]
    return {
        "id": "root",
        "children": [{
            "id": "iss",
            "kind": "if",
            "label": "AI 투자",
            "support_pct": 60,
            "affinity": affinity,
            "children": [
                {
                    "id": "f1", "kind": "fork", "edge": emp_edge, "emphasized": True,
                    "branch_pct": 70, "label": "투자가 이어지면",
                    "children": [{
                        "id": "o1", "kind": "outcome", "label": "그러면 반도체 쪽",
                        "sector_keys": emp_sectors,
                        "watch_tickers": [{"ticker": "005930", "name": "삼성전자"}],
                        "children": [],
                    }],
                },
                {
                    "id": "f2", "kind": "fork", "edge": "alt", "emphasized": False,
                    "branch_pct": 30, "label": "꺾이면",
                    "children": [{
                        "id": "o2", "kind": "outcome", "label": "그러면 방어",
                        "sector_keys": alt_sectors,
                        "children": [],
                    }],
                },
            ],
        }],
    }


def test_extract_emphasized_only():
    impacts = climate._extract_impacts(_mini_tree())
    # risk_on: emphasized outcome only (no growth headwind)
    assert len(impacts) == 1
    assert impacts[0]["sign"] == 1.0
    assert "semiconductor" in impacts[0]["sector_keys"]
    assert "005930" in impacts[0]["tickers"]


def test_risk_off_adds_growth_headwind():
    impacts = climate._extract_impacts(_mini_tree(affinity="risk_off", emp_sectors=["defense"]))
    assert len(impacts) == 2
    assert any(i["sign"] < 0 and "semiconductor" in i["sector_keys"] for i in impacts)


def test_evaluate_boosts_score_does_not_mutate_base():
    hypo = {
        "ready": True, "as_of": "2099-01-01", "tree": _mini_tree(),
    }
    base = 0.5
    out = climate.evaluate_ticker("005930", base, hypo=hypo)
    assert out and out["label"] == "기후"
    assert out["base_score"] == 0.5
    assert out["score"] > base  # α*q 체감
    assert out["kind"] in eng.BUY_KINDS or out["kind"] == eng.HOLD or out["kind"] in eng.SELL_KINDS
    assert "봇" in out["disclaimer"]


def test_annotate_rows_leaves_kind_score():
    hypo = {"ready": True, "as_of": "2099-01-01", "tree": _mini_tree()}
    rows = [{"ticker": "005930", "score": 1.0, "kind": "BUY"}]
    # inject hypo via monkeypatch on hypothesis.get
    import signal_desk.signals.hypothesis as hyp
    orig = hyp.get
    hyp.get = lambda build_if_missing=False: hypo
    try:
        climate.annotate_rows(rows)
    finally:
        hyp.get = orig
    assert rows[0]["kind"] == "BUY"
    assert rows[0]["score"] == 1.0
    assert rows[0]["climate"] and rows[0]["climate"]["kind"]


def test_stale_hypo_hides_badge():
    hypo = {"ready": True, "as_of": "2020-01-01", "tree": _mini_tree()}
    assert climate.evaluate_ticker("005930", 1.0, hypo=hypo) is None


def test_engine_combine_unaffected():
    """기후 모듈이 engine.combine 경로에 끼지 않음."""
    import inspect
    from signal_desk.signals import engine
    src = inspect.getsource(engine.evaluate)
    assert "climate" not in src
    assert "hypothesis" not in src
