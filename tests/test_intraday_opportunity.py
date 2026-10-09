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


def test_first_completed_minute_bars_can_support_shadow_but_not_order():
    candidate = model.detect_move("kr", "005930", {"ts": 1_000, "price": 100},
                                  {"ts": 1_300, "price": 102})
    volume = {"state": "observed", "complete_bars": 10, "previous_5m_volume": 1_000,
              "recent_5m_volume": 2_000, "minute_volume_ratio": 2.0}
    decision = model.plan(candidate, volume, model.context_evidence(at=1_300))
    assert decision["status"] == "shadow" and decision["order_eligible"] is False
    assert model.plan(candidate, {**volume, "complete_bars": 9}, {})["status"] == "watch"
    assert model.plan(candidate, {**volume, "minute_volume_ratio": 1.1}, {})["status"] == "watch"
    assert model.plan(candidate, {**volume, "state": "stale_or_mismatched"}, {})["status"] == "watch"


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
    assert model.walk_forward(history)["status"] == "insufficient"


def test_walk_forward_uses_past_only_and_same_day_control():
    def row(day, net, playbook="event_continuation"):
        return {"session": (dt.date(2026, 8, 1) + dt.timedelta(days=day)).isoformat(),
                "playbook": playbook, "regime": "mixed",
                "replay": {"status": "complete", "immediate": {"net_pct": net},
                           "wait_one": {"net_pct": net}}}
    training = [row(day, 1.0) for day in range(30)]
    evaluation = [row(day, 0.5) for day in range(30, 40)]
    evaluation += [row(day, -0.5, "unexplained_surge") for day in range(30, 40)]
    assert model.walk_forward(training + evaluation, min_oos_days=10)["status"] == "measured_shadow"
    outcome = model.walk_forward(training + evaluation, min_oos_days=10)
    assert outcome["oos_days"] == 10 and outcome["mean_net_pct"] == 0.5
    assert outcome["baseline_mean_net_pct"] == 0.0
    assert outcome["order_eligible"] is False
    assert model.walk_forward(training + evaluation[:9], min_oos_days=10)["status"] == "insufficient"


def test_calibration_sampling_spans_days_not_just_recent_events(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "sample.db")
    for day in range(31):
        for item in range(5):
            db.intraday_opportunity_record(f"{day:02d}-{item}", {
                "market": "kr", "ticker": "005930", "detected_at": 1000 + day * 86400 + item,
                "session": (dt.date(2026, 8, 1) + dt.timedelta(days=day)).isoformat(),
                "decision": {"playbook": "event_continuation"}})
    sample, truncated = db.intraday_opportunities_sampled("kr", after_ts=0)
    assert len(sample) == 93 and not truncated
    assert len({row["session"] for row in sample}) == 31
    limited, truncated = db.intraday_opportunities_sampled("kr", after_ts=0, limit=20)
    assert len(limited) == 20 and truncated


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
    scan = db.intraday_opportunity_scans_recent("kr", after_ts=now - 1)[0]
    assert scan["quote_tickers"] == 1 and scan["paired_tickers"] == 1
    assert scan["price_candidates"] == 1 and scan["saved_candidates"] == 1
    assert scan["kis_snapshot_requests"] == 1 and scan["kis_snapshot_success"] == 0
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


def test_official_rank_first_pages_are_only_watchlist_codes(monkeypatch):
    calls = []
    def respond(path, tr_id, creds, params):
        calls.append((path, tr_id, params))
        if "fluctuation" in path:
            return {"rt_cd": "0", "output": [{"stck_shrn_iscd": "005930"},
                                              {"stck_shrn_iscd": "bad"}], "_tr_cont": "M"}
        return {"rt_cd": "0", "output": [{"mksc_shrn_iscd": "005930"},
                                         {"mksc_shrn_iscd": "000660"}]}
    monkeypatch.setattr(kis, "_request", respond)
    credentials = {"env": "real", "app_key": "a", "app_secret": "b", "account_no": "c", "product_cd": "01"}
    result = kis.domestic_rank_watchlist(credentials)
    assert result["candidates"] == ["005930", "000660"]
    assert result["status"] == "observed" and result["source_time_verified"] is False
    assert result["sources"]["price_rank"]["status"] == "first_page_only"
    assert [call[1] for call in calls] == ["FHPST01700000", "FHPST01710000"]
    monkeypatch.setattr(kis, "_request", lambda *args, **kwargs: None)
    assert kis.domestic_rank_watchlist(credentials)["status"] == "failed"


def test_rank_watchlist_requires_flag_and_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "rank.db")
    monkeypatch.delenv("INTRADAY_RANK_RADAR", raising=False)
    monkeypatch.setattr(service.kis, "domestic_rank_watchlist", lambda: (_ for _ in ()).throw(
        AssertionError("flag off must not request KIS")))
    assert service.refresh_rank_watchlist(now=1_000)["status"] == "off"
    assert service.ranked_tickers(now=1_000) == set()
    monkeypatch.setenv("INTRADAY_RANK_RADAR", "1")
    calls = []
    def ranked():
        calls.append(1)
        return {"status": "observed", "candidates": ["005930", "000660", "bad"], "sources": {}}
    monkeypatch.setattr(service.kis, "domestic_rank_watchlist", ranked)
    assert service.refresh_rank_watchlist(now=1_000)["status"] == "observed"
    assert service.refresh_rank_watchlist(now=1_600)["status"] == "observed"
    assert len(calls) == 1
    assert service.ranked_tickers(now=1_600) == {"005930", "000660"}
    assert service.ranked_tickers(now=2_201) == set()
    assert service.refresh_rank_watchlist(now=2_201)["status"] == "observed"
    assert len(calls) == 2


def test_historical_minute_probe_is_bounded_and_keeps_source_date_uncertainty(monkeypatch):
    calls = []
    def respond(path, tr_id, creds, params):
        calls.append((path, tr_id, params))
        return {"rt_cd": "0", "output2": [
            {"stck_cntg_hour": "145900", "stck_prpr": "100", "cntg_vol": "100",
             "stck_bsop_date": "20260714"},
            {"stck_cntg_hour": "150000", "stck_prpr": "101", "cntg_vol": "200",
             "stck_bsop_date": "20260714"},
            {"stck_cntg_hour": "150100", "stck_prpr": "102", "cntg_vol": "300",
             "stck_bsop_date": "20260713"},
            {"stck_cntg_hour": "150200", "stck_prpr": "103", "cntg_vol": "-1"},
        ]}
    monkeypatch.setattr(kis, "_request", respond)
    credentials = {"env": "real", "app_key": "a", "app_secret": "b", "account_no": "c", "product_cd": "01"}
    result = kis.domestic_historical_minute_probe("005930", "2026-07-14", credentials)
    assert result["status"] == "observed" and len(result["bars"]) == 2
    assert result["invalid_rows"] == 2 and result["date_attested_by_rows"] is False
    assert len(result["bars_sha256"]) == 64 and result["first_page_only"]
    assert calls[0][1] == "FHKST03010230"
    assert calls[0][2]["FID_INPUT_DATE_1"] == "20260714"
    assert kis.domestic_historical_minute_probe("005930", "2026-08-04", credentials)["status"] == "outside_development_window"
    assert kis.domestic_historical_minute_probe("005930", "2026-09-01", credentials)["status"] == "outside_development_window"
    assert len(calls) == 1


def test_historical_minute_day_pages_back_to_open_without_filling_gaps(monkeypatch):
    minutes = [dt.datetime(2026, 7, 14, 9, 0) + dt.timedelta(minutes=i) for i in range(391)]
    minutes = [minute for minute in minutes if minute.strftime("%H:%M:%S") != "10:17:00"]
    calls = []
    def respond(path, tr_id, creds, params):
        cursor = params["FID_INPUT_HOUR_1"]
        calls.append(cursor)
        rows = [minute for minute in minutes if minute.strftime("%H%M%S") <= cursor][-120:]
        return {"rt_cd": "0", "output2": [
            {"stck_cntg_hour": minute.strftime("%H%M%S"), "stck_prpr": "100",
             "cntg_vol": "3", "stck_bsop_date": "20260714"} for minute in reversed(rows)]}
    monkeypatch.setattr(kis, "_request", respond)
    credentials = {"env": "real", "app_key": "a", "app_secret": "b", "account_no": "c", "product_cd": "01"}
    result = kis.domestic_historical_minute_day("005930", "2026-07-14", credentials)
    assert result["status"] == "observed_day"
    assert result["bar_count"] == 390 and len(result["pages"]) == 4
    assert result["first_time"] == "09:00:00" and result["last_time"] == "15:30:00"
    assert "10:17:00" not in {bar["time"] for bar in result["bars"]}
    assert result["missing_minutes_not_filled"] is True
    assert calls[0] == "153000" and calls == sorted(calls, reverse=True)


def test_historical_minute_day_rejects_missing_date_and_cursor_ignoring_provider(monkeypatch):
    credentials = {"env": "real", "app_key": "a", "app_secret": "b", "account_no": "c", "product_cd": "01"}
    def missing_date(path, tr_id, creds, params):
        return {"rt_cd": "0", "output2": [
            {"stck_cntg_hour": "153000", "stck_prpr": "100", "cntg_vol": "3"}]}
    monkeypatch.setattr(kis, "_request", missing_date)
    assert kis.domestic_historical_minute_day("005930", "2026-07-14", credentials)["status"] == "unverified_source"
    def ignores_cursor(path, tr_id, creds, params):
        return {"rt_cd": "0", "output2": [
            {"stck_cntg_hour": "153000", "stck_prpr": "100", "cntg_vol": "3",
             "stck_bsop_date": "20260714"}]}
    monkeypatch.setattr(kis, "_request", ignores_cursor)
    assert kis.domestic_historical_minute_day("005930", "2026-07-14", credentials)["status"] == "unverified_source"


def test_admin_minute_day_caches_only_complete_bounded_result(tmp_path, monkeypatch):
    from signal_desk import api
    monkeypatch.setattr(db, "DB", tmp_path / "day.db")
    monkeypatch.setattr(api, "_admin_or_403", lambda request: None)
    calls = []
    def fake_day(ticker, session):
        calls.append((ticker, session))
        return {"status": "observed_day", "bar_count": 1, "bars": [{"time": "09:00:00"}]}
    monkeypatch.setattr(kis, "domestic_historical_minute_day", fake_day)
    assert api.intraday_minute_day_get(object(), "005930", "2026-07-14")["cached"] is False
    assert api.intraday_minute_day_get(object(), "005930", "2026-07-14")["cached"] is True
    assert calls == [("005930", "2026-07-14")]
    try:
        api.intraday_minute_day_get(object(), "000660", "2026-07-14")
        assert False, "uncached day must share global throttle with probe"
    except api.HTTPException as exc:
        assert exc.status_code == 429
    try:
        api.intraday_minute_day_get(object(), "005930", "2026-08-04")
        assert False, "protected date must be rejected before cache lookup"
    except api.HTTPException as exc:
        assert exc.status_code == 422
    try:
        api.intraday_minute_probe_get(object(), "005930", "2026-08-04")
        assert False, "single-page probe must use the same date fence"
    except api.HTTPException as exc:
        assert exc.status_code == 422


def test_admin_minute_probe_caches_one_bounded_page(tmp_path, monkeypatch):
    from signal_desk import api
    monkeypatch.setattr(db, "DB", tmp_path / "probe.db")
    monkeypatch.setattr(api, "_admin_or_403", lambda request: None)
    calls = []
    def fake_probe(ticker, session):
        calls.append((ticker, session))
        return {"status": "observed", "bars": [{"time": "15:00:00", "price": 100, "volume": 1}]}
    monkeypatch.setattr(kis, "domestic_historical_minute_probe", fake_probe)
    assert api.intraday_minute_probe_get(object(), "005930", "2026-07-14")["cached"] is False
    assert api.intraday_minute_probe_get(object(), "005930", "2026-07-14")["cached"] is True
    assert calls == [("005930", "2026-07-14")]
    try:
        api.intraday_minute_probe_get(object(), "000660", "2026-07-14")
        assert False, "uncached probe should be throttled"
    except api.HTTPException as exc:
        assert exc.status_code == 429


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


def test_first_kis_snapshot_with_completed_bars_is_not_stuck_on_watch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "minute.db")
    now = int(time.time())
    db.intraday_quotes_record("kr", {"005930": {"price": 100}}, ts=now - 300)
    db.intraday_quotes_record("kr", {"005930": {"price": 102}}, ts=now)
    monkeypatch.setattr(service.kis, "domestic_market_snapshot", lambda ticker: {
        "received_at": now, "price": 102, "cumulative_volume": 20_000,
        "day_high": 103, "provider": "kis"})
    monkeypatch.setattr(service.kis, "domestic_investor_estimate", lambda ticker: None)
    monkeypatch.setattr(service.kis, "domestic_completed_minute_volumes", lambda ticker: {
        "received_at": now, "previous_5m_volume": 1_000, "recent_5m_volume": 2_000,
        "ratio": 2.0, "complete_bars": 10, "last_complete_minute": "observed"})
    event = service.scan_market("kr", now=now)[0]
    assert event["volume"]["complete_bars"] == 10
    assert event["decision"]["status"] == "shadow"
    assert event["decision"]["order_eligible"] is False
    scan = db.intraday_opportunity_scans_recent("kr", after_ts=now - 1)[0]
    assert scan["kis_snapshot_success"] == 1 and scan["kis_minute_success"] == 1
    assert scan["volume_supported"] == 1


def test_empty_scan_explains_missing_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "empty-scan.db")
    now = int(time.time())
    assert service.scan_market("us", now=now) == []
    scan = db.intraday_opportunity_scans_recent("us", after_ts=now - 1)[0]
    assert scan["status"] == "no_quote_rows" and scan["price_candidates"] == 0
    assert db.intraday_opportunity_scans_recent("kr", after_ts=now - 1) == []
    db.intraday_opportunities_prune(older_than_ts=now + 1)
    assert db.intraday_opportunity_scans_recent("us", after_ts=now - 1) == []


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
