"""Core R3 numerical, leakage, missing-data and rejection boundaries."""

import copy
from types import SimpleNamespace

import pytest

from signal_desk.signals import research_ridge as ridge
from scripts.measure.kr_ridge_selection_lab import label_at, portfolio_stats


def _plan():
    return {"train_start": "2024-04-01", "train_end": "2024-09-20", "fit_through": "2024-09-30",
            "evaluation_start": "2024-10-02", "features": ["x"], "alpha_weighted_mean_objective": .1,
            "standardized_clip": 8}


def _rows():
    return [{"date": "2024-04-01", "ticker": t, "exit_date": "2024-04-09", "features": [x], "target": y}
            for t, x, y in (("A", -1, -1), ("B", 1, 1))]


def test_ridge_closed_form_positive_control_and_no_output_ceiling():
    model = ridge.fit(_rows(), _plan())
    assert model["coefficients"][0] == pytest.approx(0)
    assert model["coefficients"][1] == pytest.approx(1 / 1.1)
    assert ridge.predict([[4]], model)[0] > 3
    assert model["last_label_exit"] == "2024-04-09"


@pytest.mark.parametrize("change", ["future_row", "future_label", "duplicate", "infinite_target"])
def test_rejects_time_leakage_duplicates_and_nonfinite_targets(change):
    rows = _rows()
    if change == "future_row":
        rows[0]["date"] = "2024-10-02"
    elif change == "future_label":
        rows[0]["exit_date"] = "2024-10-02"
    elif change == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    else:
        rows[0]["target"] = float("inf")
    with pytest.raises(ValueError):
        ridge.fit(rows, _plan())


def test_missing_columns_and_future_transform_do_not_refit_preprocessing():
    p = {**_plan(), "features": ["x", "absent"]}
    rows = [{**r, "features": r["features"] + [None]} for r in _rows()]
    model = ridge.fit(rows, p)
    saved = copy.deepcopy(model)
    ridge.predict([[999999, 999999], [None, None]], model)
    assert model == saved and model["medians"][1] == 0
    assert ridge.transform([[None, None]], model)[0, -2:].tolist() == [1, 1]


def test_changing_later_prices_and_cap_does_not_change_asof_features():
    from signal_desk import market_clock
    from scripts.measure.kr_ridge_selection_lab import features_at
    from scripts.measure.price_challenger_lab import PricePanel
    import datetime as dt
    start = dt.date(2023, 3, 2)
    sessions = [(start + dt.timedelta(days=i)).isoformat() for i in range(420)
                if market_clock.is_session("kr", start + dt.timedelta(days=i))][:256]
    by_day = {d: {f"{i:05d}0": {"ISU_CD": f"{i:05d}0", "ISU_NM": f"회사{i}",
                "MKTCAP": 1000000 - i, "TDD_OPNPRC": 100 + j, "TDD_CLSPRC": 101 + j,
                "ACC_TRDVOL": 1000} for i in range(200)} for j, d in enumerate(sessions)}
    day = sessions[252]
    panel = PricePanel({d: list(rows.values()) for d, rows in by_day.items()})
    before = features_at(day, panel, by_day, [], set())[0]
    assert len(before) == 200
    for d in sessions[253:]:
        for r in by_day[d].values():
            r["TDD_CLSPRC"] *= 999
            r["MKTCAP"] *= 999
    changed = PricePanel({d: list(rows.values()) for d, rows in by_day.items()})
    assert features_at(day, changed, by_day, [], set())[0] == before


def test_date_weighting_does_not_give_large_cross_sections_more_target_weight():
    rows = [{"date": "2024-04-01", "ticker": "A", "exit_date": "2024-04-09", "features": [0], "target": 0}]
    rows += [{"date": "2024-04-02", "ticker": str(i), "exit_date": "2024-04-10", "features": [0], "target": 2}
             for i in range(10)]
    assert ridge.fit(rows, _plan())["coefficients"][0] == pytest.approx(1)


def test_open_to_open_label_horizon_and_visible_share_change():
    sessions = [f"2024-04-{i:02d}" for i in range(1, 9)]
    panel = SimpleNamespace(index={s: i for i, s in enumerate(sessions)}, sessions=sessions,
                            bars={(s, "A"): (100 + i, 9999) for i, s in enumerate(sessions)})
    days = {s: {"A": {"LIST_SHRS": "100"}} for s in sessions}
    result = label_at(sessions[0], "A", panel, days, 5)
    assert result["exit_date"] == sessions[6] and result["target"] == pytest.approx(106 / 101 - 1)
    days[sessions[3]]["A"]["LIST_SHRS"] = "200"
    assert label_at(sessions[0], "A", panel, days, 5)["state"] == "share_history_unverified"


def test_partial_sell_costs_reconcile_and_return_cannot_hide_oversell():
    fills = [{"side": "buy", "ticker": "A", "shares": 10, "price": 100, "session": "2024-10-04",
              "decision_input_id": "r3/ridge/0/2024-10-02/hash"},
             {"side": "sell", "ticker": "A", "shares": 5, "price": 110},
             {"side": "sell", "ticker": "A", "shares": 5, "price": 120}]
    pnl = 5 * (110 * .99 - 101) + 5 * (120 * .99 - 101)
    result = {"state": "complete", "net_return_pct": 0, "cash_end": 10_000_000 + pnl,
              "fills": fills, "equity_curve": []}
    stats = portfolio_stats(result, .01)
    assert stats["completed_trades"] == 1 and stats["pnl_by_entry_month"] == {"2024-10": pytest.approx(pnl)}
    result["fills"][-1]["shares"] = 6
    with pytest.raises(ValueError, match="exceeds"):
        portfolio_stats(result, .01)


def _grade_plan():
    return {"phase_count": 5, "scenarios": ["45bp_full", "75bp_full", "120bp_full", "75bp_half", "75bp_none", "75bp_delay1"],
            "max_drawdown_floor_pct": -10, "min_completed_trades_each_phase": 12, "min_entry_dates_each_phase": 6,
            "min_distinct_tickers_each_phase": 6, "max_ticker_absolute_pnl_share": .5, "min_positive_entry_months": 2}


def _scenarios():
    out = {}
    for scenario in _grade_plan()["scenarios"]:
        out[scenario] = {}
        for arm in ("ridge", "legacy", "cap", "random", "cash"):
            value = 2 if arm == "ridge" else 1
            if scenario == "75bp_none" or arm == "cash":
                value = 0
            out[scenario][arm] = [{"state": "complete", "net_return_pct": value, "max_drawdown_pct": -2,
                "completed_trades": 20, "entry_dates": 10, "distinct_tickers": 10, "top_ticker_pnl_share": .2,
                "pnl_by_entry_month": {"2024-10": 100, "2024-11": 100}} for _ in range(5)]
    return out


def test_admission_positive_control_worst_phase_and_missing_path():
    scenarios = _scenarios()
    assert ridge.grade(scenarios, _grade_plan())["decision"] == "research_shadow_candidate"
    scenarios["75bp_full"]["ridge"][4]["net_return_pct"] = -1
    assert ridge.grade(scenarios, _grade_plan())["decision"] == "rejected"
    scenarios["75bp_full"]["random"][0]["state"] = "incomplete_corporate_action"
    assert ridge.grade(scenarios, _grade_plan())["decision"] == "inconclusive_source_quality"


def _listing_source(*, cb=False):
    from scripts.measure.kr_ridge_action_sources import IDENTITIES
    ticker = "395400" if cb else "323410"
    isin, issuer = IDENTITIES[ticker]
    amount = "-기명식 보통주 5주" if cb else ": 5주 -기명식 보통주 2주 -기명식 보통주 3주"
    effective = "-2024년10월21일(2주) -2024년10월22일(3주)" if cb else ": 2024년10월21일"
    method = "국내CB전환" if cb else "주식매수선택권 행사"
    source = (f"◎ {issuer} 보통주 추가상장 ① 주식의 종류와 수 {amount} ② 1주의 발행가액 : 100원 "
              f"③ 발행일 {effective} ④ 배당기산일 : 2024년01월01일 ⑤ 증자방법 : {method} "
              f"⑥ 상장일 : 2024년11월06일 ⑦ 코드 {isin} (단축코드:A{ticker})").encode()
    entry = {"ticker": ticker, "krx_change_session": "2024-11-06", "session": "2024-11-01",
             "original_url": "https://kind.krx.co.kr/external/test.htm"}
    priority = {"listed_shares_before": 1000, "listed_shares_after": 1005, "previous_session": "2024-11-05"}
    return source, entry, priority


@pytest.mark.parametrize("cb", [False, True])
def test_reviewed_listing_batches_preserve_holder_quantity_not_action_free_coverage(cb):
    from scripts.measure.kr_ridge_action_sources import parse_fact
    source, entry, priority = _listing_source(cb=cb)
    fact = parse_fact(source, entry, priority, "2026-10-17T00:00:00+00:00")
    assert fact.kind == ("convertible_bond_issuance" if cb else "option_exercise_issuance")
    assert fact.holder_quantity_effect == "unchanged" and fact.coverage_state == "partial"
    assert fact.effective_on == "2024-10-21"
    if cb:
        assert "2024년10월22일" in fact.source_span


@pytest.mark.parametrize("change", ["identity", "amount", "date", "method", "duplicate_identity"])
def test_reviewed_listing_refuses_inconsistent_or_ambiguous_source(change):
    from scripts.measure.kr_ridge_action_sources import parse_fact
    source, entry, priority = _listing_source()
    if change == "identity":
        source = source.replace(b"KR7323410001", b"KR7323410002")
    elif change == "amount":
        priority["listed_shares_after"] += 1
    elif change == "date":
        entry["krx_change_session"] = "2024-11-07"
    elif change == "method":
        source = source.replace("주식매수선택권 행사".encode(), "주식매수선택권 행사 및 무상증자".encode())
    else:
        source += b" KR7323410001 (" + "단축코드:A323410)".encode()
    with pytest.raises(ValueError):
        parse_fact(source, entry, priority, "2026-10-17T00:00:00+00:00")
