"""점수 동결 감지 — 시세 갱신이 멈추면 문턱·분위를 어떻게 바꿔도 매일 같은 결과가 나온다.

2026-07-26 진단에서 로컬 PIT 스냅샷의 일간 순위상관이 0.988~1.000, technical·momentum의
같은 종목 일간 표준편차가 0.0000이었다. 이게 프로덕션에서도 벌어지고 있으면 캘리브레이션
작업 전체가 무의미해지므로, 사람이 눈으로 발견하지 않아도 진단 화면에 뜨게 한다.
"""

import importlib

from signal_desk.signals.engine import SignalResult


def _sig(ticker, score):
    return SignalResult(ticker=ticker, name=ticker, score=score, kind="HOLD", confidence=0.5,
                        technical_score=0.0, fundamental_score=0.0, has_fundamental=False)


def _store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import store as store_module
    importlib.reload(store_module)
    return store_module


def test_no_snapshot_is_unknown_not_ok(tmp_path, monkeypatch):
    """스냅샷이 없으면 '정상'이 아니라 '판정 불가'다."""
    store = _store(tmp_path, monkeypatch)
    out = store.signal_drift()
    assert out["available"] is False and out["frozen"] is None


def test_single_day_cannot_judge(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.snapshot_signals([_sig("A", 1.0)], date="2026-07-24")
    assert store.signal_drift()["frozen"] is None


def test_identical_scores_flagged_frozen(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    sigs = [_sig(f"T{i}", 1.0 + i * 0.01) for i in range(20)]
    store.snapshot_signals(sigs, date="2026-07-23")
    store.snapshot_signals(sigs, date="2026-07-24")   # 시세 정지 → 점수 동일
    out = store.signal_drift()
    assert out["frozen"] is True
    assert out["pairs"][-1]["changed_pct"] == 0.0
    assert "시세 갱신 중단" in out["note"]


def test_moving_scores_not_frozen(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.snapshot_signals([_sig(f"T{i}", 1.0 + i * 0.01) for i in range(20)], date="2026-07-23")
    store.snapshot_signals([_sig(f"T{i}", 1.2 + i * 0.01) for i in range(20)], date="2026-07-24")
    out = store.signal_drift()
    assert out["frozen"] is False and out["pairs"][-1]["changed_pct"] == 100.0


def test_partial_drift_below_threshold_is_frozen(tmp_path, monkeypatch):
    """일부만 변하는 것도 동결이다 — 20종목 중 1종목(5%)만 움직이면 시세가 살아있다고 볼 수 없다."""
    store = _store(tmp_path, monkeypatch)
    store.snapshot_signals([_sig(f"T{i}", 1.0) for i in range(20)], date="2026-07-23")
    store.snapshot_signals([_sig(f"T{i}", 1.5 if i == 0 else 1.0) for i in range(20)],
                           date="2026-07-24")
    assert store.signal_drift()["frozen"] is True
