"""두뇌 레이어 엔진 헬스 스냅샷 — 파이프라인 그래프 모델 + 헬스 스코어 + 규칙 기반 findings."""

from signal_desk import brain

_FRESH_OK = [{"key": k, "label": k, "stale": False, "age_hours": 3, "rows": 100}
             for k in ("prices", "us_prices", "fundamentals", "flows", "short", "macro")]
_WEIGHTS = {"weight_technical": 0.35, "weight_fundamental": 0.30, "weight_valuation": 0.15,
            "weight_reversion": 0.20, "weight_flow": 0.20, "weight_quality": 0.15,
            "weight_momentum": 0.20, "weight_short": 0.15}


def _ids(snap):
    return {n["id"] for n in snap["nodes"]}


def test_snapshot_shape_and_core_nodes():
    snap = brain.build(_FRESH_OK, {"ready": False}, _WEIGHTS, is_ready=True)
    assert set(snap) >= {"score", "level", "nodes", "edges", "findings", "summary"}
    assert 0 <= snap["score"] <= 100
    ids = _ids(snap)
    # 8 팩터 + 4 게이트 + 엔진 + 트래커 + 두뇌 2노드
    for k in ("technical", "flow", "short", "momentum"):
        assert f"fac:{k}" in ids
    for k in ("regime", "trend", "earnings", "kb_veto"):
        assert f"gate:{k}" in ids
    assert {"engine", "tracker", "diagnose", "propose"} <= ids
    # 엣지: 모든 팩터 → 엔진, 엔진 → 트래커 → 진단 → 제안 → (루프)엔진
    assert {"source": "engine", "target": "tracker"} in snap["edges"]
    assert any(e.get("kind") == "loop" for e in snap["edges"])


def test_stale_source_flagged():
    fresh = [dict(f) for f in _FRESH_OK]
    fresh[3]["stale"] = True   # flows stale
    snap = brain.build(fresh, {"ready": False}, _WEIGHTS, is_ready=True)
    flow_src = next(n for n in snap["nodes"] if n["id"] == "src:flows")
    assert flow_src["status"] == "stale"
    assert any("오래됨" in f["text"] for f in snap["findings"])


def test_negative_ic_factor_warned():
    acc = {"ready": True, "factor_ic": {"short": -0.05},
           "coverage": {"matured_primary": 40, "dates": 25}}
    snap = brain.build(_FRESH_OK, acc, _WEIGHTS, is_ready=True)
    short_fac = next(n for n in snap["nodes"] if n["id"] == "fac:short")
    assert short_fac["status"] == "warn"
    assert any("IC 음수" in f["text"] for f in snap["findings"])


def test_low_sample_ic_not_warned():
    # 표본<20이면 음수 IC라도 판정 보류(warn 아님)
    acc = {"ready": True, "factor_ic": {"short": -0.05}, "coverage": {"matured_primary": 5}}
    snap = brain.build(_FRESH_OK, acc, _WEIGHTS, is_ready=True)
    short_fac = next(n for n in snap["nodes"] if n["id"] == "fac:short")
    assert short_fac["status"] != "warn"


def test_tracker_idle_when_not_ready():
    snap = brain.build(_FRESH_OK, {"ready": False, "coverage": {"dates": 2}}, _WEIGHTS, is_ready=True)
    tr = next(n for n in snap["nodes"] if n["id"] == "tracker")
    assert tr["status"] == "idle"
    assert next(n for n in snap["nodes"] if n["id"] == "diagnose")["status"] == "idle"


def test_tracker_ready_activates_brain():
    acc = {"ready": True, "factor_ic": {}, "coverage": {"matured_primary": 30}}
    snap = brain.build(_FRESH_OK, acc, _WEIGHTS, is_ready=True)
    assert next(n for n in snap["nodes"] if n["id"] == "diagnose")["status"] == "ok"


def test_consensus_idle_when_empty():
    fresh = _FRESH_OK + [{"key": "consensus", "label": "컨센", "stale": False, "rows": 0, "age_hours": 1}]
    snap = brain.build(fresh, {"ready": False}, _WEIGHTS, is_ready=True)
    con = next(n for n in snap["nodes"] if n["id"] == "src:consensus")
    assert con["status"] == "idle"
