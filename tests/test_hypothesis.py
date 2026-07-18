"""시황 가설(#6) — 지지도 정규화·빌드·엔진 무접촉."""

from signal_desk.signals import hypothesis


def test_normalize_sums_to_100():
    pct = hypothesis._normalize({"a": 0.5, "b": 0.3, "c": 0.2})
    assert sum(pct.values()) == 100
    assert pct["a"] >= pct["b"] >= pct["c"]


def test_normalize_handles_zeros():
    pct = hypothesis._normalize({"a": 0.0, "b": 0.0, "c": 0.0})
    assert sum(pct.values()) == 100


def test_build_tree_shape(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # store / kb / cycle 의존 최소화
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
    kids = out["tree"]["children"]
    assert len(kids) == 3
    assert sum(c["support_pct"] for c in kids) == 100
    assert all("지지도" not in c["label"] for c in kids)  # 라벨은 시나리오명
    assert kids[0]["evidence"]
    # 시그널 엔진 필드와 무관
    assert "kind" not in out and "score" not in out


def test_get_uses_cache(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db
    payload = {"ready": True, "as_of": "2026-07-18", "tree": {"id": "root", "children": []},
               "disclaimer": "x"}
    db.kv_set(hypothesis._KV_KEY, payload)
    monkeypatch.setattr(hypothesis, "refresh", lambda: (_ for _ in ()).throw(AssertionError("캐시 있으면 refresh 금지")))
    assert hypothesis.get()["as_of"] == "2026-07-18"


def test_hypothesis_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import api
    fake = {"ready": True, "as_of": "2026-07-18", "tree": {"id": "root", "children": []},
            "disclaimer": "x"}
    monkeypatch.setattr(api.hypothesis, "get", lambda build_if_missing=True: fake)
    assert api.hypothesis_get()["ready"] is True
