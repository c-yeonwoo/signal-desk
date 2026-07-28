"""장중 시그널 전이 로그 — kind 바뀔 때만 기록."""

from signal_desk import db
from signal_desk.signals import episode_state as es


def test_records_first_buy_and_demote(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    today = "2026-07-28"
    es.observe_rows(
        [{"ticker": "A", "kind": "BUY", "price": 1000.0, "hold_tag": None}],
        market="kospi", today=today, now_ts=1_000,
    )
    st = es.load("kospi", today)
    assert st["A"]["first_buy_ts"] == 1_000
    assert st["A"]["first_buy_px"] == 1000.0
    assert st["A"].get("demoted_at") is None

    # 같은 kind면 미갱신
    es.observe_rows(
        [{"ticker": "A", "kind": "BUY", "price": 1100.0}],
        market="kospi", today=today, now_ts=2_000,
    )
    st = es.load("kospi", today)
    assert st["A"]["first_buy_ts"] == 1_000
    assert st["A"]["first_buy_px"] == 1000.0

    es.observe_rows(
        [{"ticker": "A", "kind": "HOLD", "price": 900.0, "hold_tag": "급락",
          "reasons": ["[급락] 1일 -10%"]}],
        market="kospi", today=today, now_ts=3_000,
    )
    st = es.load("kospi", today)
    assert st["A"]["demoted_at"] == 3_000
    assert st["A"]["demote_reason"] == "급락"
    assert st["A"]["first_buy_px"] == 1000.0  # 당일 발동가 유지


def test_annotate_adjusts_today_entry_fire_price(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    today = "2026-07-28"
    es.observe_rows(
        [{"ticker": "A", "kind": "BUY", "price": 1000.0}],
        market="kospi", today=today, now_ts=1_000,
    )
    rows = [{
        "ticker": "A", "kind": "BUY", "price": 1200.0,
        "entry": {
            "fire_date": today, "fire_price": 1200.0, "run_up_pct": 0.0,
            "quality": "fresh", "quality_ko": "신선", "age_days": 0,
        },
    }]
    es.annotate_rows(rows, market="kospi", today=today)
    assert rows[0]["entry"]["fire_price"] == 1000.0
    assert rows[0]["entry"]["run_up_pct"] == 20.0
    assert rows[0]["episode"]["first_buy_ts"] == 1_000


def test_demote_reason_prefers_hold_tag():
    assert es.demote_reason({"hold_tag": "악재"}) == "악재"
    assert es.demote_reason({"reasons": ["[급락] x"]}) == "급락"
