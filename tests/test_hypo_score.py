"""이슈 흐름 신선도·사후 채점 — 방법론 속성을 못박는다."""

from __future__ import annotations

from signal_desk.signals import hypo_score as hs


def test_age_becomes_a_verdict_not_a_raw_timestamp():
    """원시 타임스탬프는 "12일 전"을 말해주지 않는다.

    프로덕션에서 `2026-07-26 18:46` 이 메타 6개 사이에 묻혀 있었고, 그래서 12일 전 트리가
    `최근 이슈 흐름` 이라는 이름으로 떠 있었다.
    """
    fresh = hs.staleness("2026-08-07", today="2026-08-07")
    assert fresh["age_days"] == 0 and fresh["stale"] is False
    old = hs.staleness("2026-07-26", today="2026-08-07")
    assert old["age_days"] == 12 and old["stale"] is True
    assert "12일 전" in old["label"] and "다시 생성" in old["label"]
    # 나이가 늘면 신선도는 단조 감소한다.
    prev = 101.0
    for d in ("2026-08-07", "2026-08-05", "2026-08-01", "2026-07-26"):
        v = hs.staleness(d, today="2026-08-07")["fresh_pct"]
        assert v < prev
        prev = v
    # 0의 이유 — 없는 것과 못 읽는 것을 가른다.
    assert "없습니다" in hs.staleness(None)["label"]
    assert "읽을 수 없" in hs.staleness("not-a-date")["label"]


def test_half_life_matches_the_kb_news_convention():
    """반감기를 기능마다 다르게 두면 같은 자료가 화면마다 다른 신선도를 갖는다."""
    assert hs.staleness("2026-08-07", today="2026-08-07")["half_life_days"] == 4.0


def test_score_needs_a_baseline_and_refuses_small_samples():
    """기준선 없는 비율은 내지 않는다 — 상승장에서는 아무 업종이나 오른다."""
    # 성숙 표본이 요건 미달이면 값 대신 이유.
    r = hs.score([{"id": 1, "built_at": "2026-07-01T09:00", "as_of": "2026-07-01",
                   "sectors": ["semiconductor"], "tickers": ["A"]}],
                 {"A": (["2026-07-01", "2026-07-02"], [100.0, 101.0])})
    assert r["lift_pp"] is None and r["mean_picked_pct"] is None
    assert "성숙한 흐름" in r["blocked_reason"]
    # 조건 채점은 하지 않는다는 사실을 밝힌다(지어내지 않는다).
    assert r["conditions_scored"] is False and "복원할 수 없" in r["conditions_note"]
    assert "기준선" in r["basis"] and "리프트" in r["basis"]


def test_lift_is_picked_minus_all_tickers_on_the_same_day():
    """리프트는 **같은 날 전 종목 평균** 대비다 — 다른 날과 비교하면 시장 드리프트가 섞인다."""
    dates = [f"2026-07-{d:02d}" for d in range(1, 26)]
    # 지목 종목 P 는 매일 +1%, 나머지 두 종목은 0% → 리프트가 양수로 나와야 한다.
    up = [100.0 * (1.01 ** i) for i in range(len(dates))]
    flat = [50.0] * len(dates)
    closes = {"P": (dates, up), "X": (dates, flat), "Y": (dates, flat)}
    runs = [{"id": i, "built_at": f"2026-07-{i:02d}T09:00", "as_of": f"2026-07-{i:02d}",
             "sectors": ["s"], "tickers": ["P"]} for i in range(1, 4)]
    r = hs.score(runs, closes, horizon=2, min_runs=3)
    assert r["matured"] == 3
    # 기준선에는 **지목 종목도 포함**된다(시장 평균의 정의) → 3종목 중 하나만 오르면 1/3.
    # 그래서 기준선은 0이 아니고, 리프트는 그만큼 보수적으로 나온다.
    assert r["mean_picked_pct"] > 0
    assert 0 < r["mean_baseline_pct"] < r["mean_picked_pct"]
    assert r["lift_pp"] > 0
    assert abs(r["lift_pp"] - (r["mean_picked_pct"] - r["mean_baseline_pct"])) < 0.01
    # 각 행이 기준선을 함께 들고 있어야 한다(행만 보고도 검증 가능해야 한다).
    for row in r["rows"]:
        if row["picked_pct"] is not None:
            assert row["baseline_pct"] is not None
            assert abs(row["lift_pp"] - (row["picked_pct"] - row["baseline_pct"])) < 0.01


def test_verdict_comes_from_the_shared_implementation():
    """판정 통계를 두 곳에 두면 갈라진다 — `accuracy.diff_verdict` 를 공유한다."""
    from pathlib import Path
    src = Path("src/signal_desk/signals/hypo_score.py").read_text(encoding="utf-8")
    assert "accuracy.diff_verdict(" in src
    assert "accuracy._forward_returns(" in src, "채점 규약을 자체 구현하면 실측과 갈라진다"
