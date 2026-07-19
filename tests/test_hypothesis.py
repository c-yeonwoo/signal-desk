"""최근 이슈 흐름(#6) — 수동 Sonnet · 캐시 only GET · 쌍갈림."""

from signal_desk.signals import hypothesis


def test_normalize_sums_to_100():
    pct = hypothesis._normalize({"a": 0.5, "b": 0.3, "c": 0.2})
    assert sum(pct.values()) == 100
    assert pct["a"] >= pct["b"] >= pct["c"]


def test_normalize_handles_zeros():
    pct = hypothesis._normalize({"a": 0.0, "b": 0.0, "c": 0.0})
    assert sum(pct.values()) == 100


def test_cond_ok_and_fork_weight():
    inds = [
        {"key": "VIXCLS", "value": 28.0, "change": 2.0},
        {"key": "NASDAQCOM", "value": 18000, "change": -1.2},
    ]
    ok = hypothesis._cond_ok(
        {"metric": "VIXCLS", "op": ">=", "threshold": 25},
        indicators=inds, macro_bias="비우호", regime_name="약세",
    )
    assert ok is True
    w = hypothesis._fork_weight(
        [
            {"metric": "VIXCLS", "op": ">=", "threshold": 25, "label": "VIX"},
            {"metric": "NASDAQCOM", "op": "chg<", "threshold": 0, "label": "나스닥↓"},
        ],
        indicators=inds, macro_bias="비우호", regime_name="약세",
    )
    assert w > 0.9
    cur = hypothesis._condition_snapshot(
        [{"metric": "VIXCLS", "op": ">=", "threshold": 25}],
        indicators=inds, macro_bias="비우호", regime_name="약세",
    )
    assert "VIXCLS" in cur


def test_validate_llm_branches():
    raw = {
        "branches": [
            {
                "id": "hot_ai",
                "label": "AI 투자 지속",
                "affinity": "risk_on",
                "assumptions": ["CAPEX 유지"],
                "sector_keys": ["semiconductor", "not_a_sector"],
                "evidence_query": "AI 반도체",
                "children": [
                    {
                        "id": "t1",
                        "kind": "fork",
                        "edge": "path",
                        "label": "나스닥 강세",
                        "conditions": [
                            {"metric": "NASDAQCOM", "op": "chg>", "threshold": 0, "label": "↑"},
                            {"metric": "HACK", "op": ">", "threshold": 1, "label": "bad"},
                        ],
                        "children": [
                            {
                                "id": "o1",
                                "kind": "outcome",
                                "label": "반도체 관심",
                                "sector_keys": ["semiconductor"],
                                "evidence_query": "반도체",
                            }
                        ],
                    },
                    {
                        "label": "변동성 확대",
                        "edge": "alt",
                        "conditions": [
                            {"metric": "VIXCLS", "op": ">=", "threshold": 25, "label": "VIX"},
                        ],
                        "children": [{"label": "방어", "sector_keys": ["defense"]}],
                    },
                ],
            },
            {
                "label": "위험회피",
                "affinity": "risk_off",
                "sector_keys": ["defense"],
                "evidence_query": "VIX",
                "children": [
                    {
                        "label": "VIX 상승",
                        "edge": "path",
                        "conditions": [
                            {"metric": "VIXCLS", "op": ">=", "threshold": 20, "label": "VIX"},
                        ],
                        "children": [{"label": "방어", "sector_keys": ["defense"]}],
                    }
                ],
            },
        ]
    }
    out = hypothesis._validate_llm_branches(raw)
    assert out and len(out) == 2
    assert "not_a_sector" not in out[0]["sector_keys"]
    assert all(c["metric"] != "HACK" for c in out[0]["children"][0]["conditions"])
    # 항상 path/alt 쌍
    for br in out:
        edges = {c["edge"] for c in br["children"]}
        assert edges == {"path", "alt"}
        assert all(c["kind"] == "fork" for c in br["children"])


def _stub_macro_cycle(monkeypatch):
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


def test_build_fallback_tree(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _stub_macro_cycle(monkeypatch)
    out = hypothesis.build(source="fallback")
    assert out["ready"] and out["source"] == "fallback"
    assert hypothesis._KV_KEY == "hypo:v4:latest"
    kids = out["tree"]["children"]
    assert len(kids) == 3
    assert all(c["kind"] == "if" for c in kids)
    assert sum(c["support_pct"] for c in kids) == 100
    forks = kids[0]["children"]
    assert len(forks) == 2
    assert {f["edge"] for f in forks} == {"path", "alt"}
    assert all(f["kind"] == "fork" for f in forks)
    assert sum(f["branch_pct"] for f in forks) == 100
    assert sum(1 for f in forks if f["emphasized"]) == 1
    assert "status" not in forks[0]
    assert "가정" not in out["disclaimer"]
    assert "검증" not in out["disclaimer"]


def test_get_no_auto_build(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    called = {"n": 0}

    def boom():
        called["n"] += 1
        raise AssertionError("refresh 금지")

    monkeypatch.setattr(hypothesis, "refresh", boom)
    out = hypothesis.get(build_if_missing=True)
    assert out["ready"] is False
    assert called["n"] == 0


def test_get_uses_v4_cache(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db
    payload = {
        "ready": True, "as_of": "2026-07-18", "source": "llm",
        "tree": {"id": "root", "children": [{"id": "x", "kind": "if", "children": []}]},
        "disclaimer": "x",
    }
    db.kv_set(hypothesis._KV_KEY, payload)
    monkeypatch.setattr(
        hypothesis, "refresh",
        lambda: (_ for _ in ()).throw(AssertionError("캐시 있으면 refresh 금지")),
    )
    assert hypothesis.get()["as_of"] == "2026-07-18"


def test_refresh_fails_without_silent_fallback(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _stub_macro_cycle(monkeypatch)
    monkeypatch.setattr(
        hypothesis, "_llm_draft_templates",
        lambda: (None, "claude-sonnet-test", "LLM JSON을 해석하지 못했습니다."),
    )
    out = hypothesis.refresh()
    assert out["ready"] is False
    assert "LLM" in out["reason"]
    assert hypothesis.get()["ready"] is False  # 실패 시 캐시 안 씀


def test_validate_allows_single_issue():
    out = hypothesis._validate_llm_branches({
        "branches": [{
            "label": "금리 이야기만 큼",
            "affinity": "consumer",
            "sector_keys": ["finance"],
            "evidence_query": "금리",
            "detail": "FEDFUNDS",
            "children": [{
                "label": "금리가 더 안 오르면",
                "edge": "path",
                "conditions": [{"metric": "FEDFUNDS", "op": "chg<=", "threshold": 0, "label": "금리"}],
                "children": [{"label": "금융·내수를 볼 만함", "sector_keys": ["finance"]}],
            }],
        }],
    })
    assert out and len(out) == 1
    assert out[0]["detail"] == "FEDFUNDS"
    assert {c["edge"] for c in out[0]["children"]} == {"path", "alt"}


def test_parse_llm_json_strips_fence_and_trailing_comma():
    raw = hypothesis._parse_llm_json(
        '```json\n{"branches":[{"label":"a","affinity":"risk_on",}],}\n```'
    )
    assert raw and "branches" in raw


def test_validate_coerces_messy_llm_fields():
    out = hypothesis._validate_llm_branches({
        "branches": [{
            "label": "반도체 이야기가 큼",
            "affinity": 0.9,
            "assumptions": "사람이 부족하다",
            "sector_keys": ["반도체", "AI"],
            "children": [{
                "label": "투자가 이어지면",
                "edge": "그런데",
                "children": [],
            }],
        }],
    })
    assert out and out[0]["affinity"] == "risk_on"
    assert "semiconductor" in out[0]["sector_keys"] or "ai_datacenter" in out[0]["sector_keys"]
    edges = {c["edge"] for c in out[0]["children"]}
    assert edges == {"path", "alt"}
    assert all(c["children"] for c in out[0]["children"])  # outcome 자동 보강


def test_refresh_uses_llm_templates(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _stub_macro_cycle(monkeypatch)
    fake = hypothesis._validate_llm_branches({
        "branches": [
            {
                "label": "반도체 투자가 이어질까", "affinity": "risk_on",
                "sector_keys": ["semiconductor"], "evidence_query": "A",
                "children": [
                    {
                        "label": "시장이 안심하면", "edge": "path",
                        "conditions": [{"metric": "VIXCLS", "op": "<", "threshold": 20, "label": "calm"}],
                        "children": [{"label": "관련 업종을 눈여겨볼 만함", "sector_keys": ["semiconductor"]}],
                    },
                    {
                        "label": "불안이 커지면", "edge": "alt",
                        "conditions": [{"metric": "VIXCLS", "op": ">=", "threshold": 25, "label": "fear"}],
                        "children": [{"label": "방어 쪽을 볼 만함", "sector_keys": ["defense"]}],
                    },
                ],
            },
            {
                "label": "불안이 커질까", "affinity": "risk_off",
                "sector_keys": ["defense"], "evidence_query": "B",
                "children": [
                    {
                        "label": "변동성이 커지면", "edge": "path",
                        "conditions": [{"metric": "VIXCLS", "op": ">=", "threshold": 25, "label": "fear"}],
                        "children": [{"label": "방어 쪽을 볼 만함", "sector_keys": ["defense"]}],
                    },
                    {
                        "label": "변동성이 잦아들면", "edge": "alt",
                        "conditions": [{"metric": "VIXCLS", "op": "<", "threshold": 20, "label": "calm"}],
                        "children": [{"label": "위험선호 회복", "sector_keys": ["semiconductor"]}],
                    },
                ],
            },
        ]
    })
    monkeypatch.setattr(hypothesis, "_llm_draft_templates", lambda: (fake, "claude-sonnet-test", None))
    out = hypothesis.refresh()
    assert out["source"] == "llm"
    assert out["model"] == "claude-sonnet-test"
    assert out["tree"]["label"] == "최근 이슈 흐름"
    assert len(out["tree"]["children"]) == 2
    assert sum(c["support_pct"] for c in out["tree"]["children"]) == 100
    forks = out["tree"]["children"][0]["children"]
    assert sum(f["branch_pct"] for f in forks) == 100
    assert any(f["emphasized"] for f in forks)


def test_hypothesis_endpoint_no_autobuild(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import api
    monkeypatch.setattr(api.hypothesis, "get", lambda build_if_missing=False: {
        "ready": False, "reason": "없음",
    })
    assert api.hypothesis_get()["ready"] is False


def test_daily_kb_collect_skips_hypothesis_refresh():
    import inspect
    from signal_desk import api
    src = inspect.getsource(api._daily_kb_collect)
    assert "hypothesis.refresh" not in src
