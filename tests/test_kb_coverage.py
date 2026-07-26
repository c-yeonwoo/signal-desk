"""KB 근거 커버리지 shadow — 원문 있는 매수 후보 vs 없는 후보의 실현수익률 비교.

게이트(출처 등록·스팸·무관 심사)는 '형식적 신뢰도'만 보고 주장이 맞았는지는 채점하지 않는다.
그래서 KB의 값어치는 이렇게 사후 실측으로만 말할 수 있다 — 판정은 유의성(diff_verdict) 공용 규칙.
"""

import pytest

from signal_desk import db, store
from signal_desk.signals import kb_coverage


def _snapshot(dates_scores):
    """(date, ticker, kind) 목록을 PIT 스냅샷으로 적재."""
    from signal_desk.signals.engine import SignalResult

    for date, rows in dates_scores:
        sigs = [SignalResult(ticker=t, name=t, score=1.5, kind=k, confidence=0.5,
                             technical_score=0.1, fundamental_score=0.0,
                             has_fundamental=False, reasons=[]) for t, k in rows]
        store.snapshot_signals(sigs, date=date)


def _closes(ticker_rets: dict[str, float], dates: list[str]):
    """{ticker: 일별 수익률} → {ticker: (dates, closes)}. 진입은 시그널일 다음 거래일."""
    out = {}
    for t, r in ticker_rets.items():
        px, closes = 100.0, []
        for _ in dates:
            closes.append(px)
            px *= (1 + r)
        out[t] = (dates, closes)
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    return tmp_path


def test_docs_group_beats_no_docs_when_returns_differ(env):
    dates = [f"2026-07-{d:02d}" for d in range(1, 26)]
    # 원문이 있는 종목(A*)은 매일 +1%, 없는 종목(B*)은 -1%
    for t in ("A1", "A2", "A3"):
        for i in range(3):
            db.kb_document_add(t, f"{t} 기사{i}", "요약", f"http://x/{t}/{i}",
                               "naver_news", "", "뉴스")
    _snapshot([("2026-07-02", [("A1", "BUY"), ("A2", "BUY"), ("A3", "BUY"),
                               ("B1", "BUY"), ("B2", "BUY"), ("B3", "BUY")])])
    closes = {**_closes({t: 0.01 for t in ("A1", "A2", "A3")}, dates),
              **_closes({t: -0.01 for t in ("B1", "B2", "B3")}, dates)}
    out = kb_coverage.shadow(closes, horizon=5, min_samples=3)
    assert out["n"] == 3 and out["n_control"] == 3
    assert out["delta_pct"] > 0 and out["verdict_ready"] is True
    assert out["buy_rows"] == 6
    # 편향은 숨기지 않는다 — 수집 대상 교락은 항상 붙는다
    assert any("수집했나" in c for c in out["caveats"])


def test_pit_column_is_preferred_over_reconstruction(env):
    """문서 수를 나중에 `fetched`로 재구성하면 prune이 지운 문서만큼 과소집계된다.
    스냅샷에 kb_docs를 남겼으면 그 값을 쓰고, 재구성 행 수를 함께 보고해야 한다."""
    dates = [f"2026-07-{d:02d}" for d in range(1, 26)]
    for i in range(4):
        db.kb_document_add("A1", f"기사{i}", "요약", f"http://x/{i}", "naver_news", "", "뉴스")
    _snapshot([("2026-07-02", [("A1", "BUY"), ("B1", "BUY")])])
    df = store.load_signal_history()
    assert "kb_docs" in df.columns                      # 스냅샷이 커버리지를 PIT로 남긴다
    assert int(df[df["ticker"] == "A1"].iloc[0]["kb_docs"]) == 4
    assert int(df[df["ticker"] == "B1"].iloc[0]["kb_docs"]) == 0

    closes = {**_closes({"A1": 0.01}, dates), **_closes({"B1": -0.01}, dates)}
    out = kb_coverage.shadow(closes, horizon=5, min_samples=1)
    assert out["pit_rows"] == 2 and out["reconstructed_rows"] == 0
    assert not any("재구성" in c for c in out["caveats"])

    # 문서를 지운 뒤에도(prune 상황) PIT 열이 있으므로 결과가 흔들리지 않는다
    c = db.conn(); c.execute("DELETE FROM kb_entries"); c.commit(); c.close()
    again = kb_coverage.shadow(closes, horizon=5, min_samples=1)
    assert again["n"] == 1 and again["n_control"] == 1


def test_immature_rows_are_excluded_not_zero_filled(env):
    """horizon이 안 지난 시그널을 0%로 채우면 최근 시그널이 결과를 끌어당긴다 — 표본에서 뺀다."""
    dates = ["2026-07-01", "2026-07-02", "2026-07-03"]     # 5거래일 성숙 불가
    for i in range(3):
        db.kb_document_add("A1", f"기사{i}", "요약", f"http://x/{i}", "naver_news", "", "뉴스")
    _snapshot([("2026-07-02", [("A1", "BUY"), ("B1", "BUY")])])
    out = kb_coverage.shadow(_closes({"A1": 0.01, "B1": -0.01}, dates),
                             horizon=5, min_samples=3)
    assert out["matured"] == 0 and out["verdict_ready"] is False
    assert out["blocked_reason"]                            # 0에는 이유가 붙는다


def test_verdict_blocked_when_no_buy_rows(env):
    _snapshot([("2026-07-02", [("A1", "HOLD"), ("B1", "SELL")])])
    out = kb_coverage.shadow({}, horizon=5)
    assert out["ready"] is False and "매수" in out["blocked_reason"]


def test_coverage_now_counts_universe_share(env, monkeypatch):
    monkeypatch.setattr(store, "load_universe",
                        lambda: [{"ticker": "A1", "name": "A"}, {"ticker": "B1", "name": "B"}])
    db.kb_document_add("A1", "기사", "요약", "http://x/1", "naver_news", "", "뉴스")
    cov = kb_coverage.coverage_now()
    assert cov["universe"] == 2 and cov["with_docs"] == 1 and cov["with_docs_pct"] == 50.0
