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


# X1(2026-08-06): 팩터 IC는 **날짜 단위** 통계로 온다. `factor_ic[k]`는 날짜 요건·유의성을
# 통과했을 때만 값이고, 못 통과하면 None + `factor_ic_stats[k].blocked_reason`이다.
def _ic(k, ic, *, n_dates=25, p=0.001, blocked=None):
    return {"factor_ic": {k: ic},
            "factor_ic_stats": {k: {"ic": ic, "ic_mean": ic, "n_dates": n_dates,
                                    "independent_dates": n_dates // 20, "p": p,
                                    "significant": ic is not None,
                                    "blocked_reason": blocked}},
            "ic_min_dates": 20}


def test_negative_ic_factor_warned():
    acc = {"ready": True, "coverage": {"matured_primary": 40, "dates": 25},
           **_ic("fundamental", -0.05)}
    snap = brain.build(_FRESH_OK, acc, _WEIGHTS, is_ready=True)
    fund_fac = next(n for n in snap["nodes"] if n["id"] == "fac:fundamental")
    assert fund_fac["status"] == "warn"
    assert any("엔진 기대 방향과 반대" in f["text"] for f in snap["findings"])
    # 경고 문구에 **날짜 수**가 들어가야 한다 — 행 수를 쓰면 하루치 200종목이 표본 200으로 읽힌다.
    assert any("25거래일" in f["text"] for f in snap["findings"]), snap["findings"]


def test_inverse_factor_negative_ic_is_the_expected_direction():
    """valuation·short 원값은 낮을수록 좋다 — 음수 IC를 역방향 경고로 오인하지 않는다."""
    acc = {"ready": True, "coverage": {"matured_primary": 40, "dates": 25},
           **_ic("valuation", -0.05)}
    snap = brain.build(_FRESH_OK, acc, _WEIGHTS, is_ready=True)
    val = next(n for n in snap["nodes"] if n["id"] == "fac:valuation")
    assert val["status"] != "warn"
    assert "정상 방향" in val["metric"]

    wrong = {"ready": True, "coverage": {"matured_primary": 40, "dates": 25},
             **_ic("valuation", 0.05)}
    wrong_snap = brain.build(_FRESH_OK, wrong, _WEIGHTS, is_ready=True)
    wrong_val = next(n for n in wrong_snap["nodes"] if n["id"] == "fac:valuation")
    assert wrong_val["status"] == "warn"
    assert any("엔진 기대 방향과 반대" in f["text"] for f in wrong_snap["findings"])


def test_timing_factor_low_ic_not_warned():
    # technical/reversion은 타이밍·게이트 역할(횡단면 IC≈0 정상) → 음수 IC라도 warn 아님, info만
    acc = {"ready": True, "coverage": {"matured_primary": 40, "dates": 25},
           **_ic("technical", -0.05)}
    snap = brain.build(_FRESH_OK, acc, _WEIGHTS, is_ready=True)
    tech = next(n for n in snap["nodes"] if n["id"] == "fac:technical")
    assert tech["status"] != "warn"
    assert any("타이밍/게이트" in f["text"] for f in snap["findings"])


def test_low_sample_ic_not_warned():
    """IC 날짜가 모자라면 음수라도 warn이 아니고, **왜 못 쟀는지**가 노드·findings에 남는다.

    옛 버전은 `matured_primary`(행) < 20으로 판단했다. 그러면 하루치 200종목이 표본 200이 되어
    문턱을 통과했고, 실제로 `short IC −0.148`이 단 하루에서 나온 값이었다.
    """
    acc = {"ready": True, "coverage": {"matured_primary": 900, "dates": 5},
           **_ic("short", None, n_dates=5, p=None, blocked="IC 날짜 5/20일 — 판정 불가")}
    snap = brain.build(_FRESH_OK, acc, _WEIGHTS, is_ready=True)
    short_fac = next(n for n in snap["nodes"] if n["id"] == "fac:short")
    assert short_fac["status"] != "warn"
    assert "5/20일" in short_fac["metric"]
    assert any("5/20일" in f["text"] for f in snap["findings"]), snap["findings"]
    # 트래커 노드도 행 수만 자랑하지 않는다 — IC 날짜를 같이 낸다.
    tr = next(n for n in snap["nodes"] if n["id"] == "tracker")
    assert "IC 5/20일" in tr["metric"] and tr["status"] == "idle"


def test_tracker_idle_when_not_ready():
    snap = brain.build(_FRESH_OK, {"ready": False, "coverage": {"dates": 2}}, _WEIGHTS, is_ready=True)
    tr = next(n for n in snap["nodes"] if n["id"] == "tracker")
    assert tr["status"] == "idle"
    assert next(n for n in snap["nodes"] if n["id"] == "diagnose")["status"] == "idle"


def test_tracker_ready_activates_brain():
    acc = {"ready": True, "factor_ic": {}, "coverage": {"matured_primary": 30}}
    snap = brain.build(_FRESH_OK, acc, _WEIGHTS, is_ready=True)
    assert next(n for n in snap["nodes"] if n["id"] == "diagnose")["status"] == "ok"


def test_zero_weight_factor_is_not_counted_as_active():
    weights = {**_WEIGHTS, "weight_technical": 0.0}
    snap = brain.build(_FRESH_OK, {"ready": False}, weights, is_ready=True)
    tech = next(n for n in snap["nodes"] if n["id"] == "fac:technical")
    assert tech["status"] == "candidate" and "비활성" in tech["metric"]
    assert snap["operational"]["active_factors"] == 7


def test_operational_health_and_predictive_power_are_separate():
    acc = {"ready": True, "coverage": {"matured_primary": 100, "dates": 31},
           "factor_ic": {"score": -0.07},
           "factor_ic_stats": {"score": {"ic": -0.07, "ic_mean": -0.07,
                                                 "n_dates": 100, "independent_dates": 5,
                                                 "p": 0.001, "significant": True}}}
    snap = brain.build(_FRESH_OK, acc, _WEIGHTS, is_ready=True)
    assert snap["operational"]["level"] == "ok"
    assert snap["predictive"]["status"] == "adverse"
    assert snap["predictive"]["score_ic"] == -0.07
    assert snap["level"] == snap["operational"]["level"]
    assert "운영 건강" in snap["summary"]


def test_predictive_power_is_unproven_when_independent_windows_are_too_few():
    acc = {"ready": True, "coverage": {"matured_primary": 100, "dates": 31},
           "factor_ic": {"score": -0.07},
           "factor_ic_stats": {"score": {
               "ic": -0.07, "ic_mean": -0.07, "n_dates": 31,
               "independent_dates": 1, "min_independent_dates": 5,
               "p": 0.001, "significant": True,
               "blocked_reason": "독립 관측 1/5개 — h20 중첩 창 판정 보류"}}}
    snap = brain.build(_FRESH_OK, acc, _WEIGHTS, is_ready=True)
    assert snap["predictive"]["status"] == "unproven"
    assert snap["predictive"]["score_ic"] is None
    assert snap["predictive"]["ic_mean"] == -0.07
    assert snap["operational"]["level"] == "ok"


def test_consensus_idle_when_empty():
    fresh = _FRESH_OK + [{"key": "consensus", "label": "컨센", "stale": False, "rows": 0, "age_hours": 1}]
    snap = brain.build(fresh, {"ready": False}, _WEIGHTS, is_ready=True)
    con = next(n for n in snap["nodes"] if n["id"] == "src:consensus")
    assert con["status"] == "idle"
