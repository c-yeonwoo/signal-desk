from signal_desk.signals import meta_entry as me
from signal_desk.broker import execution


def test_triple_barrier_uses_first_hit_not_terminal_return():
    cfg = me.TripleBarrierConfig(horizon_days=3, profit_take_pct=0.10, stop_loss_pct=-0.07)
    # +10% 목표를 먼저 찍고 이후 하락해도 승리 라벨이다.
    label = me.triple_barrier(100, [100, 111, 90, 95], cfg)
    assert label is not None
    assert (label.label, label.exit_offset, label.exit_reason) == (1, 1, "take_profit")


def test_strict_meta_labels_require_pit_session_and_score_net_of_costs():
    cfg = me.TripleBarrierConfig(horizon_days=2)
    days = ["2026-09-23", "2026-09-28", "2026-09-29", "2026-09-30"]
    record = {"date": days[0], "ticker": "005930", "kind": "BUY", "score": 1.0,
              "session_valid": True, "exchange_session": days[0], "bar_asof": days[0],
              "observed_at": "2026-09-23T07:00:00+00:00"}
    prices = {"005930": (days, [100.0, 100.0, 100.0, 100.1])}
    assumptions = execution.cost_assumptions("kr")
    rows = me.build_labeled_rows([record], prices, cfg, strict_pit=True,
                                 market="kr", cost_assumptions=assumptions)
    assert len(rows) == 1
    assert rows[0]["return_pct"] > 0 and rows[0]["net_return_pct"] < 0
    assert rows[0]["label"] == 0
    assert rows[0]["net_return_2x_cost_pct"] < rows[0]["net_return_pct"]
    assert me.build_labeled_rows([{**record, "bar_asof": "2026-09-22"}], prices, cfg,
                                 strict_pit=True, market="kr", cost_assumptions=assumptions) == []
    assert me.build_labeled_rows([{**record, "observed_at": "2026-09-29T07:00:00+00:00"}], prices, cfg,
                                 strict_pit=True, market="kr", cost_assumptions=assumptions) == []
    assert me.build_labeled_rows([{**record, "market": "us"}], prices, cfg,
                                 strict_pit=True, market="kr", cost_assumptions=assumptions) == []
    assert me.build_labeled_rows([record], {"005930": (days[:2] + days[3:],
                                                  [100.0, 100.0, 100.1])}, cfg,
                                 strict_pit=True, market="kr", cost_assumptions=assumptions) == []


def test_meta_maturity_explains_zero_labels_without_backdating_legacy_history():
    cfg = me.TripleBarrierConfig()
    valid = {"date": "2026-09-28", "ticker": "005930", "kind": "BUY",
             "market": "kr", "session_valid": True, "exchange_session": "2026-09-28",
             "bar_asof": "2026-09-28", "observed_at": "2026-09-28T07:00:00+00:00"}
    legacy = {"date": "2026-07-09", "ticker": "000660", "kind": "BUY"}
    pending = me.maturity_diagnostics([legacy, valid], cfg, market="kr",
                                      as_of_session="2026-09-29", labels=0)
    assert pending == {"status": "awaiting_horizon", "pit_buy_rows": 1,
                       "first_pit_buy_session": "2026-09-28",
                       "earliest_possible_label_session": "2026-10-29"}
    assert me.maturity_diagnostics([legacy], cfg, market="kr", as_of_session="2026-09-29",
                                   labels=0)["status"] == "no_pit_buy_candidates"
    assert me.maturity_diagnostics([valid], cfg, market="kr", as_of_session="2026-10-29",
                                   labels=0)["status"] == "check_price_alignment"


def test_purged_folds_exclude_labels_overlapping_test_start():
    rows = [
        {"entry_index": 0, "label_end_index": 5, "label": 1},
        {"entry_index": 1, "label_end_index": 2, "label": 0},
        {"entry_index": 6, "label_end_index": 8, "label": 1},
        {"entry_index": 9, "label_end_index": 11, "label": 0},
    ]
    folds = me.purged_folds(rows, folds=2, embargo_days=1)
    # second test starts at index 6: label ending at 5 still embargo boundary에 걸려 train이 아니다.
    assert folds[1] == ([1], [2, 3])


def test_oof_abstains_until_a_purged_history_exists():
    rows = [{"entry_index": i, "label_end_index": i, "label": i % 2,
             "kind": "BUY", "pre_run_bucket": "fresh", "score_bucket": "q5"}
            for i in range(20)]
    estimates = me.oof_estimates(rows, folds=4, embargo_days=1, min_train=5)

    assert any(r["status"] == "abstain_insufficient_train" for r in estimates)
    assert any(r["status"] == "shadow" for r in estimates)
    # 모든 예측 행은 해당 테스트 시작 전 완결된 행만 train에 갖는다(첫 블록에는 0개).
    assert estimates[0]["train_n"] == 0


def test_promotion_uses_non_overlapping_blocks_and_requires_lower_lift():
    rows = []
    for day in range(0, 160, 20):
        # 선택군은 매번 승리, 비선택군은 매번 패배. 같은 날짜 안에서만 비교한다.
        rows.extend([
            {"entry_index": day, "label": 1, "probability": 0.8, "lcb": 0.6},
            {"entry_index": day, "label": 0, "probability": 0.4, "lcb": 0.3},
        ])
    assessment = me.promotion_assessment(rows, horizon_days=20, min_blocks=8)

    assert assessment["effective_blocks"] == 8
    assert assessment["status"] == "positive"
    assert assessment["lower_lift_pp"] > 0


def test_positive_shadow_status_is_notified_once_per_transition(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import api
    sent = []
    monkeypatch.setattr(api, "_meta_entry_shadow", lambda market: {
        "promotion": {"status": "positive", "effective_blocks": 8, "lower_lift_pp": 2.5}})
    monkeypatch.setattr(api.notify, "enqueue", lambda text, **kwargs: sent.append((text, kwargs)) or True)
    monkeypatch.setattr(api.notify, "drain", lambda: {})

    api._maybe_notify_meta_entry_shadow("kr")
    api._maybe_notify_meta_entry_shadow("kr")
    assert len(sent) == 1
    assert "OOS 8블록" in sent[0][0]
