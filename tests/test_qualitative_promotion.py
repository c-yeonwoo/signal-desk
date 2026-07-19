"""P3 정성 shadow 승격 — combine 미반영 · 게이트 · 모드 저장."""

import datetime

import pytest

from signal_desk import db, signalcfg
from signal_desk.signals import accuracy


def _series(start_date: datetime.date, n: int = 50, start_px: float = 100.0, step: float = 1.0):
    dates = [(start_date + datetime.timedelta(days=k)).isoformat() for k in range(n)]
    closes = [start_px + step * k for k in range(n)]
    return dates, closes


def _closes(start=100.0, n=90, step=1.0):
    return _series(datetime.date(2026, 1, 1), n=n, start_px=start, step=step)


def _monotonic_qual_rows(n: int = 100):
    """정성↑ → 수익↑ 표본. 시그널일을 하루씩 밀어 워크포워드 구간이 생기게."""
    closes, rows = {}, []
    base = datetime.date(2026, 1, 1)
    for i in range(n):
        step = (i - n / 2) * 0.3
        d, c = _series(base + datetime.timedelta(days=i), n=40, step=step)
        t = f"T{i}"
        closes[t] = (d, c)
        rows.append({
            "date": d[0], "ticker": t, "kind": "HOLD",
            "qualitative": float(i) / n, "momentum": 0, "technical": 0,
            "fundamental": 0, "valuation": 50, "reversion": 0, "flow": 0, "quality": 0,
        })
    return rows, closes


def test_promotion_gates_pass_on_strong_ic():
    rows, closes = _monotonic_qual_rows(100)
    m = accuracy.qualitative_promotion_metrics(rows, closes, primary=5)
    assert m["sample_count"] == 100
    assert m["overall_ic"] is not None and m["overall_ic"] > 0.2
    assert m["gates"]["min_samples"]["pass"] is True
    assert m["gates"]["overall_ic"]["pass"] is True
    assert m["gates"]["walk_forward"]["pass"] is True
    assert m["eligible_for_priority_or_threshold"] is True


def test_promotion_fails_below_min_samples():
    rows, closes = _monotonic_qual_rows(40)
    m = accuracy.qualitative_promotion_metrics(rows, closes, primary=5)
    assert m["sample_count"] == 40
    assert m["gates"]["min_samples"]["pass"] is False
    assert m["eligible_for_priority_or_threshold"] is False


def test_promotion_skips_none_qualitative():
    d, c = _closes(n=40, step=1.0)
    rows = [
        {"date": "2026-01-01", "ticker": "A", "kind": "HOLD", "qualitative": None},
        {"date": "2026-01-01", "ticker": "B", "kind": "HOLD", "qualitative": 0.2,
         "momentum": 0, "technical": 0, "fundamental": 0, "valuation": 50,
         "reversion": 0, "flow": 0, "quality": 0},
    ]
    m = accuracy.qualitative_promotion_metrics(rows, {"A": (d, c), "B": (d, c)}, primary=5)
    assert m["sample_count"] == 1


def test_walk_forward_fails_if_one_window_nonpositive():
    # 전반부 양의 관계, 후반부 음의 관계 → 일부 구간 IC≤0
    closes, rows = {}, []
    base = datetime.date(2026, 1, 1)
    for i in range(80):
        step = (i - 20) * 0.4 if i < 40 else (60 - i) * 0.4
        d, c = _series(base + datetime.timedelta(days=i), n=40, step=step)
        t = f"X{i}"
        closes[t] = (d, c)
        rows.append({
            "date": d[0], "ticker": t, "kind": "HOLD",
            "qualitative": float(i), "momentum": 0, "technical": 0,
            "fundamental": 0, "valuation": 50, "reversion": 0, "flow": 0, "quality": 0,
        })
    m = accuracy.qualitative_promotion_metrics(rows, closes, primary=5)
    assert m["gates"]["min_samples"]["pass"] is True
    assert m["gates"]["walk_forward"]["pass"] is False
    assert m["eligible_for_priority_or_threshold"] is False


def test_mode_default_off_and_shadow_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    assert signalcfg.get_qualitative_mode()["mode"] == "off"
    signalcfg.set_qualitative_mode("shadow", approved_by="a@x.com", note="start",
                                  gates_snapshot={"min_samples": {"pass": False}})
    assert signalcfg.get_qualitative_mode()["mode"] == "shadow"
    assert signalcfg.get_qualitative_mode()["approved_by"] == "a@x.com"
    hist = signalcfg.qualitative_promotion_history()
    assert hist and hist[0]["after"] == "shadow"
    st = signalcfg.qualitative_promotion_status({"sample_count": 0})
    assert st["affects_combine"] is False and st["affects_bot"] is False


def test_rejects_priority_and_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    with pytest.raises(ValueError):
        signalcfg.set_qualitative_mode("priority")
    with pytest.raises(ValueError):
        signalcfg.set_qualitative_mode("threshold")


def test_qual_mode_does_not_touch_engine_weights(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    before = signalcfg.get_dict()
    signalcfg.set_qualitative_mode("shadow", approved_by="a@x.com")
    assert signalcfg.get_dict() == before
    assert "weight_qualitative" not in signalcfg.FIELDS
