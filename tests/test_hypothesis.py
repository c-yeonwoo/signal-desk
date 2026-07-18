"""시황 가설(#6) — IF/then/outcome 트리 · 지지도 · 엔진 무접촉."""

from signal_desk.signals import hypothesis


def test_normalize_sums_to_100():
    pct = hypothesis._normalize({"a": 0.5, "b": 0.3, "c": 0.2})
    assert sum(pct.values()) == 100
    assert pct["a"] >= pct["b"] >= pct["c"]


def test_normalize_handles_zeros():
    pct = hypothesis._normalize({"a": 0.0, "b": 0.0, "c": 0.0})
    assert sum(pct.values()) == 100


def test_cond_ok_and_status():
    inds = [
        {"key": "VIXCLS", "value": 28.0, "change": 2.0},
        {"key": "NASDAQCOM", "value": 18000, "change": -1.2},
    ]
    ok = hypothesis._cond_ok(
        {"metric": "VIXCLS", "op": ">=", "threshold": 25},
        indicators=inds, macro_bias="비우호", regime_name="약세",
    )
    assert ok is True
    st, cur = hypothesis._eval_status(
        [
            {"metric": "VIXCLS", "op": ">=", "threshold": 25, "label": "VIX"},
            {"metric": "NASDAQCOM", "op": "chg<", "threshold": 0, "label": "나스닥↓"},
        ],
        indicators=inds, macro_bias="비우호", regime_name="약세",
    )
    assert st == "aligned"
    assert "VIXCLS" in cur


def test_build_if_tree_shape(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hypothesis, "_evidence_for", lambda q, k=5: (0.4, [
        {"title": "뉴스", "url": "https://example.com/a", "source": "naver_news",
         "published": "2026-07-01", "ticker": "_MARKET"},
    ]))
    monkeypatch.setattr("signal_desk.store.load_macro", lambda: [
        {"key": "CPIAUCSL", "value": 2.5, "change": -0.2},
        {"key": "FEDFUNDS", "value": 4.0, "change": -0.1},
        {"key": "NASDAQCOM", "value": 18000, "change": 1.2},
        {"key": "VIXCLS", "value": 15.0, "change": -1.0},
    ])
    monkeypatch.setattr("signal_desk.store.load_price_series", lambda: {
        "005930": [100 + i * 0.1 for i in range(80)],
    })
    monkeypatch.setattr("signal_desk.reference.cycle.position", lambda ind, persist=False: {
        "ready": True, "phase_key": "expansion", "phase_name": "확장",
        "lead_sectors": ["반도체", "산업재/기계"],
    })
    monkeypatch.setattr("signal_desk.signals.regime.classify", lambda prices: {
        "ready": True, "regime": "강세",
    })

    out = hypothesis.build()
    assert out["ready"]
    assert out["disclaimer"]
    assert hypothesis._KV_KEY == "hypo:v2:latest"
    root = out["tree"]
    assert root["kind"] == "root"
    assert root.get("active_if")
    kids = root["children"]
    assert len(kids) == 3
    assert all(c["kind"] == "if" for c in kids)
    assert sum(c["support_pct"] for c in kids) == 100
    # then → outcome nested
    then_nodes = kids[0]["children"]
    assert then_nodes and then_nodes[0]["kind"] == "then"
    assert then_nodes[0]["status"] in ("aligned", "watching", "diverging", "n/a")
    assert then_nodes[0]["support_pct"] is None
    assert then_nodes[0]["children"][0]["kind"] == "outcome"
    assert "kind" not in out and "score" not in out


def test_get_uses_v2_cache(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db
    payload = {
        "ready": True, "as_of": "2026-07-18",
        "tree": {"id": "root", "children": [{"id": "ai_capex", "kind": "if", "children": []}]},
        "disclaimer": "x",
    }
    db.kv_set(hypothesis._KV_KEY, payload)
    monkeypatch.setattr(
        hypothesis, "refresh",
        lambda: (_ for _ in ()).throw(AssertionError("캐시 있으면 refresh 금지")),
    )
    assert hypothesis.get()["as_of"] == "2026-07-18"


def test_hypothesis_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import api
    fake = {"ready": True, "as_of": "2026-07-18", "tree": {"id": "root", "children": []},
            "disclaimer": "x"}
    monkeypatch.setattr(api.hypothesis, "get", lambda build_if_missing=True: fake)
    assert api.hypothesis_get()["ready"] is True
