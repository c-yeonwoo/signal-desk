"""발동 **전** 사전 상승 관측 — `entry_quality` 가 못 재는 축."""

from __future__ import annotations

from signal_desk.signals import pre_move


def _rows(n=30):
    return [{"ticker": f"T{i}", "kind": "HOLD"} for i in range(n)]


def test_measures_the_move_before_our_signal_not_after():
    """**발동일 기준으로 잰다.** 오늘 기준으로 재면 발동 후 움직임이 섞인다.

    `entry_quality.run_up_pct` 는 발동일부터를 재서 발동 당일 항상 0이다(실측: 매수권
    2종목 둘 다 `run_up_pct:0 · fresh`). 여기는 그 반대 방향 — 발동 **전** 구간이다.
    """
    closes = [100, 101, 102, 103, 104, 110, 130]        # idx5 에서 발동, 그 뒤 급등
    dates = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04",
             "2026-08-05", "2026-08-06", "2026-08-07"]
    rows = [{"ticker": "A", "kind": "BUY", "entry": {"fire_date": "2026-08-06"}}]
    pre_move.annotate(rows, closes_by={"A": closes}, dates_by={"A": dates})
    pm = rows[0]["pre_move"]
    assert pm["basis"] == "fire_date" and pm["as_of"] == "2026-08-06"
    # 발동일(110) 기준 직전 5거래일(100) → +10%. 발동 후 130 은 섞이면 안 된다.
    assert abs(pm["run_up_pct"] - 10.0) < 0.01, f"발동 후 움직임이 섞였다: {pm['run_up_pct']}"


def test_falls_back_to_today_and_says_so():
    """발동일이 없으면 오늘 기준 — **같은 필드에 다른 뜻을 섞지 않게 밝힌다.**"""
    rows = [{"ticker": "A", "kind": "HOLD"}]
    pre_move.annotate(rows, closes_by={"A": [100, 101, 102, 103, 104, 110]})
    assert rows[0]["pre_move"]["basis"] == "today"
    assert rows[0]["pre_move"]["as_of"] is None


def test_percentile_needs_a_population():
    """절대값은 국면마다 뜻이 다르다 — 백분위를 함께 낸다. 표본이 적으면 None."""
    closes_by = {f"T{i}": [100, 100, 100, 100, 100, 100 + i] for i in range(30)}
    rows = _rows(30)
    pre_move.annotate(rows, closes_by=closes_by)
    top = rows[29]["pre_move"]
    assert top["pctile"] is not None and top["pctile"] > 90, "가장 많이 오른 종목의 분위가 낮다"
    assert rows[0]["pre_move"]["pctile"] < 10

    # 모집단이 20 미만이면 백분위를 내지 않는다(없는 정밀도를 주장하지 않는다).
    few = _rows(5)
    pre_move.annotate(few, closes_by={f"T{i}": [100, 100, 100, 100, 100, 105] for i in range(5)})
    assert few[0]["pre_move"]["pctile"] is None


def test_short_series_says_why_instead_of_zero():
    """봉이 모자라면 0이 아니라 **이유**를 낸다 — 0으로 채우면 '안 올랐다'로 읽힌다."""
    rows = [{"ticker": "A", "kind": "BUY"}]
    pre_move.annotate(rows, closes_by={"A": [100, 101]})
    pm = rows[0]["pre_move"]
    assert pm["ready"] is False and "모자랍니다" in pm["reason"]
    assert "run_up_pct" not in pm


def test_annotates_every_row_not_just_buys():
    """전 종목에 붙인다 — 백분위의 분모이고, 나중에 **산 것과 안 산 것**을 비교해야 한다."""
    closes_by = {f"T{i}": [100, 100, 100, 100, 100, 101] for i in range(25)}
    rows = _rows(25)
    pre_move.annotate(rows, closes_by=closes_by)
    assert all(r["pre_move"]["ready"] for r in rows)


def test_summary_reports_observation_not_a_verdict():
    """**판정 문구를 내면 없는 근거가 생긴다.** 관측값과 표본만 낸다."""
    closes_by = {f"T{i}": [100, 100, 100, 100, 100, 100 + i] for i in range(30)}
    rows = _rows(30)
    rows[29]["kind"] = "STRONG_BUY"
    rows[28]["kind"] = "BUY"
    pre_move.annotate(rows, closes_by=closes_by)
    s = pre_move.summary(rows)
    assert s["n_buy"] == 2 and s["n_all"] == 30
    assert s["gap_pp"] > 0, "매수권이 더 오른 상태인데 gap 이 양수가 아니다"
    # 판정어를 쓰지 않는다.
    for banned in ("나쁘", "좋", "우수", "위험함", "권장"):
        assert banned not in s["note"]
    assert "판정이 아니" in s["note"]


def test_module_does_not_touch_kind_or_score():
    """관측만 한다 — `kind`·점수·봇을 건드리면 측정 전에 전략이 바뀐다(레포 규칙)."""
    import inspect
    src = inspect.getsource(pre_move)
    for banned in ('r["kind"]', "gate_blocked", "rank_eligible", 'r["score"]'):
        assert banned not in src, f"{banned} 를 건드린다"


def test_snapshot_records_pre_run_up_so_it_can_be_scored_later():
    """**PIT에 남기지 않으면 사후 채점이 불가능하다.**

    사후에 가격에서 재구성하려면 그 시점의 발동일·유니버스를 알아야 하는데 둘 다 복원이
    어렵다(KB 커버리지에서 배운 것과 같은 이유). 그리고 **None 과 0 은 다르다** —
    0으로 채우면 "안 올랐다"로 읽힌다.
    """
    import inspect
    from signal_desk import store
    src = inspect.getsource(store.snapshot_signals)
    assert '"pre_run_up_pct"' in src, "스냅샷에 사전 상승을 안 남긴다"
    assert "pre_up.get(" in src, "없는 값을 0으로 채운다면 '안 올랐다'로 읽힌다"
    helper = inspect.getsource(store._pre_run_up_by_ticker)
    assert "trailing_return_pct" in helper, "사전 상승 계산을 두 곳에 두면 갈라진다"


def test_annotate_runs_after_entry_quality_so_fire_date_exists():
    """`entry.fire_date` 를 쓰므로 `entry_quality` **뒤에** 와야 한다 — 앞이면 늘 today 기준이다."""
    import inspect
    from signal_desk import api
    src = inspect.getsource(api._annotate_entry)
    assert src.index("entry_quality.annotate_rows") < src.index("pre_move.annotate")
