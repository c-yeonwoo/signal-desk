"""트레이더 ops — SLA/리비전/horizon/편중/섹터상대/vol 사이징."""

import time

import pandas as pd

from signal_desk import db, kb
from signal_desk.signals import crowding, horizon, revision, sector_rel, vol_sizing
from signal_desk.signals.engine import SignalResult


def _sig(ticker, kind="BUY", score=1.5):
    return SignalResult(
        ticker=ticker, name=ticker, score=score, kind=kind, confidence=0.5,
        technical_score=0, fundamental_score=0, has_fundamental=False,
    )


def test_horizon_label_picks_strongest_positive():
    # 60일 +30%, 20일 +5%, 5일 -1% → 포지션
    closes = [100.0] + [100.0] * 59
    closes[0] = 100.0
    # build 61 points: idx 60 = last
    closes = [100.0] * 61
    closes[0] = 100.0          # 60d ago
    closes[40] = 125.0         # 20d ago → +4% if last=130? set carefully
    closes[55] = 128.0
    closes[60] = 130.0
    closes[0] = 100.0          # 60d: +30%
    closes[40] = 124.0         # 20d: ~+4.8%
    closes[55] = 131.0         # 5d: slightly negative
    out = horizon.compute(closes)
    assert out is not None
    assert out["label"] == "포지션"
    assert out["rets"]["60d"] is not None and out["rets"]["60d"] > 0


def test_revision_deltas_and_annotate():
    df = pd.DataFrame([
        {"ticker": "A", "date": "2026-07-01", "fwd1_eps": 1000.0, "price_target_mean": 50000.0},
        {"ticker": "A", "date": "2026-07-08", "fwd1_eps": 1100.0, "price_target_mean": 52000.0},
        {"ticker": "B", "date": "2026-07-01", "fwd1_eps": 2000.0, "price_target_mean": 10000.0},
        {"ticker": "B", "date": "2026-07-08", "fwd1_eps": 1800.0, "price_target_mean": 9000.0},
    ])
    deltas = revision.deltas_from_history(df)
    assert deltas["A"]["signal"] == 1
    assert deltas["B"]["signal"] == -1
    rows = [{"ticker": "A", "opp_tags": []}, {"ticker": "B", "opp_tags": []}]
    revision.annotate_rows(rows, deltas)
    assert "리비전상향" in rows[0]["opp_tags"]
    assert "리비전하향" in rows[1]["opp_tags"]


def test_revision_compares_same_fiscal_year_across_rollover():
    before = {"date": "2026-12-30", "fwd1_year": "202612", "fwd1_eps": 100.0,
              "fwd2_year": "202712", "fwd2_eps": 200.0}
    after = {"date": "2027-01-04", "fwd1_year": "202712", "fwd1_eps": 200.0,
             "fwd2_year": "202812", "fwd2_eps": 250.0}
    delta = revision._delta_between("A", before, after)
    assert delta["d_eps_pct"] == 0.0
    assert delta["eps_fiscal_year"] == "202712"
    assert delta["signal"] == 0
    assert delta["feature_version"] == revision.FEATURE_VERSION
    assert revision._delta_between("A", {**before, "fwd2_year": 202712.0}, after)["d_eps_pct"] == 0.0
    assert revision._delta_between("A", {"fwd1_year": "202612", "fwd1_eps": 100},
                                   {"date": "2027-01-04", "fwd1_year": "202712", "fwd1_eps": 200}) is None


def test_revision_keeps_the_full_date_panel_for_ic():
    """새 스냅샷이 들어와도 성숙한 어제 리비전을 지우지 않는다."""
    df = pd.DataFrame([
        {"ticker": t, "date": d, "fwd1_eps": eps, "price_target_mean": pt}
        for t, vals in {
            "A": [("2026-07-01", 100.0, 100.0), ("2026-07-02", 102.0, 103.0),
                  ("2026-07-03", 105.0, 107.0)],
            "B": [("2026-07-01", 100.0, 100.0), ("2026-07-02", 98.0, 97.0),
                  ("2026-07-03", 95.0, 93.0)],
        }.items()
        for d, eps, pt in vals
    ])
    panel = revision.delta_history_from_history(df)
    assert len(panel) == 4
    assert {r["date"] for r in panel} == {"2026-07-02", "2026-07-03"}
    latest = revision.deltas_from_history(df)
    assert latest["A"]["date"] == "2026-07-03" and latest["A"]["signal"] == 1
    assert latest["B"]["signal"] == -1


def test_revision_ic_is_date_cross_sectional_not_latest_snapshot_only():
    cal = [d.date().isoformat() for d in pd.bdate_range("2026-01-02", periods=50)]
    snap_dates = cal[:30]
    rows, closes_by, dates_by = [], {}, {}
    for i in range(12):
        ticker = f"T{i:02d}"
        revision_rate = (i - 5.5) * 0.01
        return_rate = (i - 5.5) * 0.001
        for j, day in enumerate(snap_dates):
            rows.append({"ticker": ticker, "date": day,
                         "fwd1_eps": 100.0 * (1.0 + revision_rate) ** j,
                         "price_target_mean": 100.0 * (1.0 + revision_rate) ** j})
        dates_by[ticker] = cal
        closes_by[ticker] = [100.0 * (1.0 + return_rate) ** j for j in range(len(cal))]
    df = pd.DataFrame(rows)
    panel = revision.delta_history_from_history(df)
    out = revision.measure_ic(panel, closes_by, dates_by, horizon=5)
    assert out["n_dates"] == 29 and out["n_pairs"] == 29 * 12
    assert out["independent_dates"] == 5
    assert out["ic"] is not None and out["ic"] > 0.9
    assert out["ready_for_score"] is True

    # 예전 구조: 종목별 최신 하나만 주면 날짜는 1개라 판정할 수 없다.
    latest_only = revision.deltas_from_history(df)
    blocked = revision.measure_ic(latest_only, closes_by, dates_by, horizon=5)
    assert blocked["n_dates"] == 1 and blocked["ic"] is None


def test_revision_ic_rank_basic():
    # 강한 양의 상관
    ic = revision.ic_rank(
        [1, 1, 1, 1, -1, -1, -1, -1],
        [0.1, 0.08, 0.12, 0.09, -0.05, -0.08, -0.02, -0.1],
    )
    assert ic is not None and ic > 0.5


def test_crowding_warns_on_sector_concentration():
    # 반도체 3종 BUY → 100% 집중
    buys = [_sig("005930"), _sig("000660"), _sig("042700")]
    out = crowding.assess(buys)
    assert out["warn"] is True
    assert out["top_sector"] == "반도체"
    assert out["top_pct"] == 100.0


def test_crowding_no_warn_small_or_diversified():
    assert crowding.assess([_sig("005930"), _sig("000660")])["warn"] is False
    mixed = [_sig("005930"), _sig("035420"), _sig("005380")]  # 반도체·플랫폼·자동차
    assert crowding.assess(mixed)["warn"] is False


def test_sector_rel_hot_flag():
    # 같은 섹터 4종 — 최고 모멘텀이 hot
    mom = {"005930": 0.9, "000660": 0.5, "042700": 0.2, "000990": 0.1}
    rows = [{"ticker": t} for t in mom]
    sector_rel.annotate_rows(rows, momentum_by=mom, flow_by={})
    by = {r["ticker"]: r["sector_rel"] for r in rows}
    assert by["005930"]["momentum_hot"] is True
    assert by["005930"]["momentum_pct"] >= 90


def test_vol_sizing_scale_clamps():
    assert vol_sizing.scale(None, 0.02) == 1.0
    assert vol_sizing.scale(0.04, 0.02) == 0.5   # 고변동 → 절반
    assert vol_sizing.scale(0.01, 0.02) == 1.5   # 저변동 → 상한
    closes = [100.0]
    for i in range(30):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 0.99))
    v = vol_sizing.realized_vol(closes)
    assert v is not None and v > 0


def test_extend_expiring_candidates_once(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = int(time.time())
    eid = db.kb_event_upsert({
        "event_key": "cand:ttl:1",
        "ticker": "005930",
        "event_type": "litigation",
        "direction": "negative",
        "severity": "serious",
        "status": "candidate",
        "decision_eligible": False,
        "summary": "후보",
        "rationale": "검토 대기",
        "expires_at": now + 3600,  # 1시간 후 만료
        "detected_at": now,
    })
    out = kb.extend_expiring_candidates(within_hours=24, extend_days=5)
    assert out["extended"] == 1
    ev = db.kb_event_get(eid)
    assert ev["expires_at"] >= now + 5 * 86400
    assert "TTL연장" in (ev["rationale"] or "")
    # 재연장 금지
    again = kb.extend_expiring_candidates(within_hours=24, extend_days=5)
    assert again["extended"] == 0


def test_sla_alert_on_expiring(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = int(time.time())
    db.kb_event_upsert({
        "event_key": "cand:sla:1",
        "ticker": "005930",
        "event_type": "litigation",
        "direction": "negative",
        "severity": "serious",
        "status": "candidate",
        "summary": "임박",
        "expires_at": now + 2 * 3600,
        "detected_at": now,
    })
    st = db.kb_event_queue_status(now=now, soon_hours=24)
    assert st["pending"] == 1 and st["expiring_soon"] == 1
    assert st["sla_alert"] is True
