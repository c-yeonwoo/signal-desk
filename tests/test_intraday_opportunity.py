import time
import datetime as dt
from zoneinfo import ZoneInfo

from signal_desk import db, intraday_opportunity_service as service
from signal_desk.broker import kis
from signal_desk.signals import intraday_opportunity as model


def test_price_jump_is_not_a_buy_and_stale_volume_cannot_upgrade():
    previous = {"ts": 1_000, "price": 100}
    current = {"ts": 1_300, "price": 102, "observation_id": "q1", "provider": "toss"}
    candidate = model.detect_move("kr", "005930", previous, current)
    assert candidate["direction"] == "surge"
    assert model.detect_move("kr", "005930", previous, {**current, "ts": 1_600}) is None
    assert model.plan(candidate, {"state": "unavailable"}, model.context_evidence(at=1_300))["status"] == "watch"
    volume = model.volume_evidence(None, {"ts": 1_301, "price": 120, "cumulative_volume": 100},
                                   detected_at=1_301, reference_price=102)
    assert volume["state"] == "stale_or_mismatched"


def test_two_playbooks_and_negative_official_event_veto():
    candidate = model.detect_move("kr", "005930", {"ts": 1_000, "price": 100},
                                  {"ts": 1_300, "price": 102})
    first = {"ts": 1_000, "price": 100, "cumulative_volume": 1_000}
    second = {"ts": 1_300, "price": 102, "cumulative_volume": 2_000, "provider": "kis"}
    volume = model.volume_evidence(first, second, detected_at=1_300, reference_price=102)
    assert volume["interval_volume"] == 1_000
    positive = model.context_evidence(at=1_300, official_event={
        "available_at": 1_200, "direction": "positive", "source_verified": True, "source_id": "DART:1"})
    assert model.plan(candidate, volume, positive)["playbook"] == "event_continuation"
    assert model.plan(candidate, volume, positive, pullback_pct=0.8)["playbook"] == "breakout_pullback"
    future_event = model.context_evidence(at=1_300, official_event={
        "available_at": 1_301, "direction": "positive", "source_verified": True})
    assert future_event["official_event"] is None
    negative = model.context_evidence(at=1_300, official_event={
        "available_at": 1_200, "direction": "negative", "source_verified": True})
    assert model.plan(candidate, volume, negative)["status"] == "avoid"
    assert model.plan({**candidate, "direction": "drop"}, volume, negative)["status"] == "review_exit"
    assert not model.plan(candidate, volume, positive)["order_eligible"]


def test_replay_uses_next_quote_and_costs_not_signal_price():
    candidate = {"detected_at": 1_000, "price": 100}
    quotes = [
        {"ts": 1_000, "price": 100},
        {"ts": 1_060, "price": 103, "observation_id": "entry"},
        {"ts": 1_120, "price": 104, "observation_id": "wait"},
        {"ts": 4_660, "price": 105, "observation_id": "exit"},
        {"ts": 4_720, "price": 106},
    ]
    result = model.replay(candidate, quotes)
    assert result["status"] == "complete"
    assert result["immediate"]["entry_observation_id"] == "entry"
    assert result["immediate"]["net_pct"] < result["immediate"]["gross_pct"]
    assert model.replay(candidate, quotes[:3])["status"] == "immature"
    day = dt.datetime(2026, 10, 8, 15, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    tomorrow = day + dt.timedelta(days=1)
    overnight = model.replay({"detected_at": int(day.timestamp()), "market": "kr",
                              "session": "2026-10-08"}, [
        {"ts": int(tomorrow.timestamp()), "price": 110},
        {"ts": int((tomorrow + dt.timedelta(hours=1)).timestamp()), "price": 120}])
    assert overnight["status"] == "immature"


def test_calibration_hides_small_sample_and_no_future_context():
    sample = {"playbook": "event_continuation", "regime": "unknown", "session": "2026-10-10",
              "replay": {"status": "complete", "immediate": {"net_pct": 1.0}, "wait_one": {"net_pct": 0.5}}}
    result = model.calibrate([sample])[("event_continuation", "unknown")]
    assert result["status"] == "insufficient" and result["positive_rate"] is None
    relation = {"available_at": 1_000, "source_verified": True, "approved": False, "direction": "positive"}
    assert model.context_evidence(at=1_300, relation=relation)["relation"] is None
    history = [{**sample, "session": (dt.date(2026, 9, 1) + dt.timedelta(days=i)).isoformat()}
               for i in range(30)]
    assert model.research_choice(history, asof_session="2026-09-01", regime="unknown")["status"] == "abstain"
    assert model.research_choice(history, asof_session="2026-10-02", regime="unknown")["status"] == "shadow_candidate"
    assert model.research_choice([sample], asof_session="2026-10-10", regime="unknown")["status"] == "abstain"


def test_scan_persists_price_only_when_kis_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "intraday.db")
    monkeypatch.setattr(service.kis, "domestic_market_snapshot", lambda ticker: None)
    now = int(time.time())
    db.intraday_quotes_record("kr", {"005930": {"price": 100, "provider": "toss"}}, ts=now - 300)
    db.intraday_quotes_record("kr", {"005930": {"price": 102, "provider": "toss"}}, ts=now)
    rows = service.scan_market("kr", now=now)
    assert len(rows) == 1
    assert rows[0]["decision"]["status"] == "watch"
    assert rows[0]["volume"]["state"] == "unavailable"
    assert service.scan_market("kr", now=now) == []  # 관측 ID가 같은 재실행은 멱등
    assert service.recent_with_replay("kr", after_ts=now - 3600)[0]["replay"]["status"] == "immature"


def test_kis_readonly_snapshot_and_unverified_flow(monkeypatch):
    monkeypatch.setattr(kis, "_request", lambda *args, **kwargs: {
        "rt_cd": "0", "output": {"stck_prpr": "102", "acml_vol": "2000", "stck_hgpr": "103"}})
    credentials = {"env": "real", "app_key": "a", "app_secret": "b", "account_no": "c", "product_cd": "01"}
    snapshot = kis.domestic_market_snapshot("005930", credentials)
    assert snapshot["cumulative_volume"] == 2000 and snapshot["day_high"] == 103
    assert snapshot["source_time_verified"] is False
    assert kis.domestic_market_snapshot("bad", credentials) is None
    monkeypatch.setattr(kis, "_request", lambda *args, **kwargs: {"rt_cd": "0", "output2": [{
        "frgn_fake_ntby_qty": "100", "orgn_fake_ntby_qty": "-200", "bsop_hour_gb": "0930"}]})
    flow = kis.domestic_investor_estimate("005930", credentials)
    assert flow["estimate_only"] and flow["source_verified"] is False
    now = dt.datetime(2026, 10, 8, 10, 20, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    bars = [{"stck_cntg_hour": f"10{minute:02d}00", "cntg_vol": str(100 if minute < 15 else 200)}
            for minute in range(10, 20)]
    monkeypatch.setattr(kis, "_request", lambda *args, **kwargs: {"rt_cd": "0", "output2": bars})
    minute = kis.domestic_completed_minute_volumes("005930", credentials, now=now)
    assert minute["ratio"] == 2.0 and minute["complete_bars"] == 10
    bars.pop(4)  # 누락 분봉을 0거래량으로 보간하지 않는다.
    assert kis.domestic_completed_minute_volumes("005930", credentials, now=now) is None


def test_volume_confirmed_scan_still_has_no_order(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "confirmed.db")
    now = int(time.time())
    db.intraday_quotes_record("kr", {"005930": {"price": 100}}, ts=now - 300)
    db.intraday_quotes_record("kr", {"005930": {"price": 102}}, ts=now)
    db.intraday_opportunity_volume_record("kr", "005930", {
        "received_at": now - 300, "price": 100, "cumulative_volume": 1_000, "provider": "kis"})
    monkeypatch.setattr(service.kis, "domestic_market_snapshot", lambda ticker: {
        "received_at": now, "price": 102, "cumulative_volume": 2_000,
        "day_high": 103, "provider": "kis"})
    monkeypatch.setattr(service.kis, "domestic_investor_estimate", lambda ticker: None)
    monkeypatch.setattr(service.kis, "domestic_completed_minute_volumes", lambda ticker: None)
    event = service.scan_market("kr", now=now)[0]
    assert event["volume"]["interval_volume"] == 1_000
    assert event["decision"]["playbook"] == "breakout_pullback"
    assert event["decision"]["order_eligible"] is False


def test_public_list_does_not_reveal_other_users_extra_followed_ticker(monkeypatch):
    from signal_desk import api
    monkeypatch.setattr(api, "_uid", lambda request: 7)
    monkeypatch.setattr(api.store, "load_universe", lambda: [{"ticker": "005930"}])
    monkeypatch.setattr(api.db, "fav_list", lambda uid: [])
    monkeypatch.setattr(api.db, "holdings_list", lambda uid: [])
    monkeypatch.setattr(api.db, "intraday_opportunities_recent", lambda *args, **kwargs: [
        {"ticker": ticker, "detected_at": 1, "move_pct": 2.0, "direction": "surge",
         "decision": {"status": "watch", "reason": "관찰"}, "volume": {}, "context": {}}
        for ticker in ("999999", "005930")])
    response = api.intraday_opportunities_get(object(), "kr")
    assert [row["ticker"] for row in response["rows"]] == ["005930"]
