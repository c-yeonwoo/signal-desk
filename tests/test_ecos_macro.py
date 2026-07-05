"""ECOS 한국 거시 + macro.read extra 블렌딩."""

from signal_desk.signals import macro
from signal_desk.ingest import ecos


def test_macro_read_merges_extra_favor_and_reasons():
    us = [{"key": "FEDFUNDS", "label": "미 기준금리", "unit": "%", "value": 4.5, "change": -0.1, "dir": -1}]
    kr = [{"key": "KR_CPI", "label": "한국 CPI", "unit": "% YoY", "value": 3.2, "change": 0.3,
           "dir": 1, "favor": -1, "reason": "[거시] 한국 CPI 3.2% — 물가 부담"}]
    out = macro.read(us, extra=kr)
    # US 금리 인하(+1) + KR CPI 부담(-1) → score 0(중립)
    assert out["score"] == 0.0 and out["bias"] == "중립"
    assert any("한국 CPI" in r for r in out["reasons"])
    assert any(i["key"] == "KR_CPI" for i in out["indicators"])   # 전광판 칩으로 합류


def test_ecos_favor_rules(monkeypatch):
    # 통계별 시계열을 목킹(최신→과거) — 금리 상승·CPI 상승 시나리오
    def fake_series(code, cycle, item, count):
        return {
            "722Y001": [("202606", 2.75), ("202605", 2.5)],          # 기준금리 인상
            "817Y002": [("20260604", 4.2), ("20260603", 4.0)],       # 국고채 상승
            # CPI 레벨(최신→과거) — YoY 상승 시나리오: 최신 120/1년전 114=+5.3%, 직전 119/114=+4.4% → change>0
            "901Y009": [("202506", 120.0), ("202505", 119.0)] + [("x", 118.0)] * 10
            + [("202406", 114.0), ("202405", 114.0), ("202404", 113.0), ("202403", 113.0)],
        }[code]
    monkeypatch.setattr(ecos, "_series", fake_series)
    items = {i["key"]: i for i in ecos.macro_indicators()}
    assert items["KR_BASE"]["favor"] == -1     # 기준금리 인상 = 비우호
    assert items["KR_TB10"]["favor"] == -1     # 국고채 상승 = 비우호
    assert items["KR_CPI"]["value"] > 2.0 and items["KR_CPI"]["favor"] == -1


def test_ecos_empty_without_key(monkeypatch):
    from signal_desk import config
    monkeypatch.setattr(config, "ecos_key", lambda: None)
    assert ecos.macro_indicators() == []
