"""퀀트 방법론 레퍼런스 카탈로그 — 두뇌 레이어가 gap→검증방법 매핑에 쓰는 구조화 지식."""

from signal_desk.reference import quant_methods as qm


def test_all_entries_well_formed():
    keys = set()
    required = {"key", "name", "category", "status", "idea", "formula",
                "addresses", "evidence", "risk", "validate", "note"}
    for m in qm.all_methods():
        assert required <= set(m), f"{m.get('key')} 필드 누락: {required - set(m)}"
        assert m["status"] in qm._STATUSES
        assert m["key"] not in keys, f"중복 key: {m['key']}"
        keys.add(m["key"])


def test_status_buckets_nonempty():
    for s in ("active", "candidate", "rejected"):
        assert qm.by_status(s), f"{s} 비어있음"


def test_active_covers_live_engine_factors():
    active = {m["key"] for m in qm.by_status("active")}
    # 엔진에 실제로 있는 8팩터 + 게이트가 카탈로그에 active로 등재돼야 함
    assert {"technical", "fundamental", "valuation_xs", "reversion", "flow",
            "quality_fscore", "momentum_12_1", "short_interest"} <= active
    assert {"regime_gate", "trend_gate", "earnings_gate", "kb_event_veto"} <= active


def test_roadmap_candidates_present():
    cand = {m["key"] for m in qm.by_status("candidate")}
    # 로드맵 후보(리비전·IC가중·PEAD)가 candidate로 등재 (sector_neutral은 v1 반영되어 active)
    assert {"estimate_revision", "ic_weighting", "earnings_surprise_pead"} <= cand


def test_rejected_have_reason():
    for m in qm.by_status("rejected"):
        assert "미채택" in m["note"]      # 왜 안 하는지 명시


def test_get_and_by_category():
    assert qm.get("sector_neutral")["status"] == "active"   # v1 반영
    assert qm.get("estimate_revision")["status"] == "candidate"
    assert qm.get("nope") is None
    assert all(m["category"] == "gate" for m in qm.by_category("gate"))
