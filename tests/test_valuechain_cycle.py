"""사이클×밸류체인 시너지 — 현재 국면 lead_sectors로 밸류체인 cycle_fit 태깅."""

from signal_desk import api, store
from signal_desk.reference import cycle


def test_cycle_position_returns_lead_sectors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pos = cycle.position([{"key": "CPIAUCSL", "value": 2.5, "change": 0.2},
                          {"key": "FEDFUNDS", "value": 5.0, "change": 0.1}])
    assert pos["ready"] and pos["phase_name"] == "확장"
    assert "반도체" in pos["lead_sectors"]
    # 지표 부족 시 lead_sectors 빈 리스트(키 존재 보장) — 확정 상태 없으면 ready False
    assert cycle.raw_position([])["lead_sectors"] == []


def test_valuechain_tags_favored_by_cycle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "load_macro",
                        lambda: [{"key": "CPIAUCSL", "value": 2.5, "change": 0.2},
                                 {"key": "FEDFUNDS", "value": 5.0, "change": 0.1}])
    r = api.valuechain_get()
    assert r["cycle"]["phase_name"] == "확장"
    fits = {s["name"]: s["cycle_fit"] for s in r["sectors"]}
    assert fits.get("반도체") == "favored"          # 확장 국면 lead_sector
    # 유리 섹터가 앞으로 정렬(첫 섹터는 favored)
    assert r["sectors"][0]["cycle_fit"] == "favored"
    assert set(fits.values()) <= {"favored", "neutral"}


def test_valuechain_neutral_when_cycle_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "load_macro", lambda: [])  # 지표 없음 → 국면 미상
    r = api.valuechain_get()
    assert r["cycle"]["ready"] in (False, None)
    assert all(s["cycle_fit"] == "neutral" for s in r["sectors"])  # 전부 중립
