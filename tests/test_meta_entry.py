from signal_desk.signals import meta_entry as me


def test_triple_barrier_uses_first_hit_not_terminal_return():
    cfg = me.TripleBarrierConfig(horizon_days=3, profit_take_pct=0.10, stop_loss_pct=-0.07)
    # +10% 목표를 먼저 찍고 이후 하락해도 승리 라벨이다.
    label = me.triple_barrier(100, [100, 111, 90, 95], cfg)
    assert label is not None
    assert (label.label, label.exit_offset, label.exit_reason) == (1, 1, "take_profit")


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
