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


def test_annotate_rows_leaves_kind_score(monkeypatch):
    hypo = {"ready": True, "as_of": "2099-01-01", "tree": _mini_tree()}
    rows = [{"ticker": "005930", "score": 1.0, "kind": "BUY"}]
    import signal_desk.signals.hypothesis as hyp
    monkeypatch.setattr(hyp, "get", lambda build_if_missing=False: hypo)
    climate.annotate_rows(rows)
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


def test_snapshot_shadow_and_summary(tmp_path, monkeypatch):
    import signal_desk.signals.hypothesis as hyp
    from signal_desk import store

    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    hypo = {"ready": True, "as_of": "2099-01-01", "tree": _mini_tree()}
    monkeypatch.setattr(hyp, "get", lambda build_if_missing=False: hypo)

    class _S:
        def __init__(self, ticker, score, kind):
            self.ticker, self.score, self.kind = ticker, score, kind

    n = climate.snapshot_shadow([_S("005930", 0.5, "HOLD")], date="2099-01-02")
    assert n >= 1
    summary = climate.shadow_summary()
    assert summary["ready"] is True
    assert summary["days"][-1]["date"] == "2099-01-02"
    assert summary["days"][-1]["n"] >= 1


def test_shadow_verdict_scores_disagreements_not_just_counts(tmp_path, monkeypatch):
    """diverge 건수만 세는 관측은 '달랐다'는 것 말고 아무것도 말하지 않는다.
    불일치 종목의 실현수익률로 채점하고, 판정은 표본 수가 아니라 유의성으로 한다."""
    import json

    from signal_desk import store

    monkeypatch.setattr(store, "CACHE_DIR", tmp_path)
    dates = [f"2099-01-{d:02d}" for d in range(1, 20)]
    rows = [{"ticker": "AAA", "base_kind": "HOLD", "clim_kind": "BUY", "q": 0.3},
            {"ticker": "BBB", "base_kind": "BUY", "clim_kind": "HOLD", "q": -0.3}]
    blob = {d: {"as_of": "2099-01-01", "n": 2, "diverge": 2, "rows": rows} for d in dates[:6]}
    (tmp_path / "climate_shadow.json").write_text(json.dumps(blob), encoding="utf-8")

    # 기후가 매수라 한 AAA는 오르고, 기존이 매수라 한 BBB는 빠진다 → 기후 쪽 우위
    closes = {"AAA": (dates, [100.0 + 3 * i for i in range(len(dates))]),
              "BBB": (dates, [100.0 - 3 * i for i in range(len(dates))])}
    v = climate.shadow_verdict(closes, horizon=5, min_samples=3)
    assert v["ready"] is True and v["matured"] == 6
    assert v["delta_pct"] > 0 and v["verdict_ready"] is True

    # 성숙 구간이 없으면 판정 불가 — 이유가 붙는다
    v2 = climate.shadow_verdict(closes, horizon=200, min_samples=3)
    assert v2["verdict_ready"] is False and v2["blocked_reason"]
