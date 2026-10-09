"""KB 구조화 이벤트 카드(P0) — DART → kb_events → sentiment_map Decision 입력."""

import datetime
import time

from signal_desk import db, kb


def _today_ymd():
    d = datetime.date.today()
    return d.isoformat(), d.strftime("%Y%m%d")


def test_classify_disclosure_severity():
    c = kb._classify_disclosure("감자 결정")
    assert c["severity"] == "critical" and c["decision_eligible"] is True
    s = kb._classify_disclosure("유상증자 결정")
    assert s["severity"] == "serious" and s["direction"] == "unknown"
    g = kb._classify_disclosure("단일판매·공급계약 체결")
    assert g["direction"] == "positive" and g["decision_eligible"] is False
    review = kb._classify_disclosure("타법인주식및출자증권취득결정(자회사의 주요경영사항)")
    assert review["event_type"] == "disclosure_review"
    assert review["direction"] == "unknown" and review["decision_eligible"] is False
    assert review["decision_action"] == "attention"
    assert kb._classify_disclosure(
        "타법인주식및출자증권취득결정(자회사의 유상증자 참여)"
    )["decision_eligible"] is False
    assert kb._classify_disclosure("분기보고서") is None


def test_stake_acquisition_filing_is_visible_but_never_an_automatic_sell(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    pub, ymd = _today_ymd()
    items = [{"title": "[공시] 타법인주식및출자증권취득결정(자회사의 주요경영사항)",
              "source": "dart", "published": pub,
              "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={ymd}000888",
              "rcept_no": f"{ymd}000888", "doc_class": "공시"}]
    assert kb.sync_disclosure_events("267250", items) == 1
    events = db.kb_events_active("267250")
    assert len(events) == 1
    assert events[0]["event_type"] == "disclosure_review"
    assert "원문 확인" in events[0]["summary"]
    assert not events[0]["decision_eligible"]
    assert db.kb_events_active("267250", decision_only=True) == []


def test_sync_disclosure_events_and_sentiment(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    pub, ymd = _today_ymd()
    items = [{
        "title": "[공시] 감자 결정",
        "source": "dart",
        "published": pub,
        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={ymd}000999",
        "rcept_no": f"{ymd}000999",
        "doc_class": "공시",
    }]
    assert kb.sync_disclosure_events("005930", items) == 1
    evs = db.kb_events_active("005930", decision_only=True)
    assert len(evs) == 1
    assert evs[0]["severity"] == "critical"
    assert evs[0]["trust_tier"] == "official"
    assert db.kb_event_evidence(evs[0]["id"])

    # digest 행이 있어야 sentiment_map 키에 포함
    db.kb_digest_set("005930", "삼성전자", 0.0, "요약", [], 1, newest_ts=int(time.time()))
    sm = kb.sentiment_map()
    assert sm["005930"]["event_risk"] is True
    assert sm["005930"]["event_severity"] == "critical"
    assert sm["005930"]["event_id"] == evs[0]["id"]


def test_no_event_without_url(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    pub, _ = _today_ymd()
    n = kb.sync_disclosure_events("005930", [{
        "title": "[공시] 상장폐지", "source": "dart", "published": pub,
        "url": "", "rcept_no": "x",
    }])
    assert n == 0
    assert db.kb_events_list() == []


def test_legacy_digest_flag_not_decision(tmp_path, monkeypatch):
    """P2: digest event_flag만으로는 Decision/매수차단 안 함."""
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    db.kb_digest_set(
        "005930", "삼성전자", -0.5, "요약", [], 1,
        newest_ts=int(time.time()), event_flag=True, event_note="횡령 의혹",
    )
    sm = kb.sentiment_map()["005930"]
    assert sm["event_risk"] is False
    assert sm["decision"].buy_blocked is False


def test_refresh_writes_events(tmp_path, monkeypatch):
    monkeypatch.setattr(kb.db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [])
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {"005930": "00126380"})
    _, ymd = _today_ymd()
    monkeypatch.setattr(kb.ingest_dart, "disclosures", lambda cc, b, e: [
        {"report_nm": "유상증자 결정", "rcept_dt": ymd, "rcept_no": f"{ymd}000111",
         "corp_code": "00126380"}])
    monkeypatch.setattr(kb, "build_digest", lambda name, items: {
        "sentiment": -0.2, "summary": "s", "points": []})
    out = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert out["updated"] == 1
    evs = db.kb_events_active("005930", decision_only=True)
    assert len(evs) == 1 and evs[0]["severity"] == "serious"
    sm = kb.sentiment_map()["005930"]
    assert sm["event_risk"] and sm["event_severity"] == "serious"
