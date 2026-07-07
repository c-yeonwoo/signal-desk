"""③ 시그널 실현 성적표 — 봇의 실제 매수 판단이 3일 뒤 얼마나 맞았나 집계."""

from signal_desk import db


def _log_resolved(ticker, score, outcome):
    db.bot_decision_log(ticker, ticker, "buy", score, "n", {"regime": "중립"}, 100.0)
    # 방금 넣은 최신 판단의 id를 찾아 사후수익 확정
    d = db.bot_decisions_recent(1)[0]
    # id가 recent엔 없으므로 ticker+최신으로 직접 갱신(테스트 편의) — set_outcome는 id 필요라 raw 갱신
    c = db.conn()
    rid = c.execute("SELECT id FROM bot_decisions WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()[0]
    c.close()
    db.bot_decision_set_outcome(rid, outcome)


def test_scorecard_aggregates_resolved_buys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _log_resolved("AAA", 2.0, 5.0)     # 이익
    _log_resolved("BBB", 1.5, -2.0)    # 손실
    _log_resolved("CCC", 1.8, 3.0)     # 이익
    db.bot_decision_log("DDD", "DDD", "buy", 1.2, "n", {}, 100.0)  # 미실현(pending)
    sc = db.bot_decision_scorecard()
    assert sc["resolved"] == 3 and sc["pending"] == 1
    assert sc["win_rate"] == round(2 / 3 * 100, 1)     # 3건 중 2건 이익
    assert sc["avg_outcome_pct"] == round((5 - 2 + 3) / 3, 2)
    assert sc["best_pct"] == 5.0 and sc["worst_pct"] == -2.0


def test_scorecard_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sc = db.bot_decision_scorecard()
    assert sc["resolved"] == 0 and sc["win_rate"] is None
