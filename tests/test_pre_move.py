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


# ─────────────── 커버리지 배지: 개수가 아니라 비중 · 성공확률 아님 ───────────────

def test_coverage_badge_shows_the_weighted_ratio_not_a_pseudo_count():
    """**게이트가 보는 값과 화면이 보여주는 값이 같아야 한다.**

    예전 배지는 `round(coverage * 8)` 로 **가중치를 개수처럼 환산**해 두 번 왜곡했다:
      ① 실제 발동 개수와 다르다 — 모멘텀(0.30)이 빠지면 7개가 발동해도 `6/8` 로 보인다
      ② 같은 `6/8` 인데 통과·차단이 갈렸다(실측 65 통과 · 2 차단)
    팩터마다 비중이 다르기 때문이다(모멘텀·실적재무 0.30 vs 밸류·체질·공매도 0.15).
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in html.split("\n") if not ln.strip().startswith("//"))
    assert "Math.round(c * 8)" not in body, "가중치를 개수로 환산해 보여준다"
    assert "근거 ${pct}%" in body, "비중 비율을 퍼센트로 보여주지 않는다"
    # 문턱은 서버가 준 값을 쓴다 — 화면에 상수로 박으면 관리자가 바꿔도 툴팁이 옛 값을 말한다.
    assert "covMinRequired" in body and "coverage.min_required" in body


def test_coverage_badge_never_reads_as_a_success_probability():
    """**"신뢰도 87%" 로 읽히면 안 된다** — 그런 base rate 를 우리는 갖고 있지 않다.

    실현 18건 · 리프트 −5.3%p · 신뢰구간 ±23%p 로 판정 불가다. 기준선 없는 비율을 성공률처럼
    내보내는 것이 이 레포의 1번 금지 사항이다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in html.split("\n") if not ln.strip().startswith("//"))
    assert "성공 확률이 아닙니다" in body, "성공률이 아니라는 문구가 없다"
    for banned in ("신뢰도 ${pct}", "적중률 ${pct}", "승률 ${pct}"):
        assert banned not in body, f"{banned} — 커버리지를 성공률처럼 이름 붙였다"


def test_coverage_emphasis_only_where_it_actually_traps():
    """**강조는 함정에만.** 낮은 근거를 전부 튀게 하면 소음이 되고, 소음은 곧 안 읽힘이다.

    실측(2026-08-08, 200종목):
      저커버리지 31건 · 그중 **매수권 0건**(구조적 — 근거가 낮으면 매수권에서 빠진다)
      그중 점수 음수 14건 — 안 살 종목에 경고를 붙이는 건 소음이다
      화면 1위: 점수 3.00 · 근거 24% · HOLD ← 목록이 점수순이라 맨 위로 올라온다

    그래서 "시그널 있는데 근거 낮음"은 조건이 **성립할 수 없고**, 남는 진짜 함정은
    **점수는 매수 문턱을 넘는데 근거가 부족한 것**이다(31 → 6건).
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in html.split("\n") if not ln.strip().startswith("//"))
    assert "covTraps" in body, "강조 조건이 분리돼 있지 않다"
    assert "low_coverage && r.score != null && r.score >= buyScoreFloor()" in body, \
        "점수 문턱을 함께 보지 않는다 — 음수 점수에도 경고가 붙는다"
    # 강조 클래스는 함정에만 — `low ? ' low'` 로 되돌아오면 안 된다.
    assert "cov-badge${covTraps(r) ? ' low' : ''}" in body
    assert "cov-badge${low ? ' low' : ''}" not in body
    # 문턱은 서버에서 — 관리자가 바꾸면 강조 대상도 같이 움직여야 한다.
    assert "selection.rank_min_score" in body


def test_high_coverage_is_never_visually_endorsed():
    """**높은 근거를 강조하면 "좋은 매수"로 읽힌다** — 근거 100%짜리 점수 −2 종목도 있다.

    커버리지는 성공 확률이 아니라고 못박아 놓고 시각적으로 뒤집으면 안 된다.
    """
    from pathlib import Path
    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in html.split("\n") if not ln.strip().startswith("//"))
    # 높은 커버리지에 붙는 별도 강조 클래스가 생기면 안 된다.
    assert ".cov-badge.high" not in body and "' high'" not in body
    # 설명은 낮으면 모두 붙는다 — 강조만 아끼는 것이지 정보를 줄이는 게 아니다.
    assert "매수 대상에서 제외됩니다" in body
