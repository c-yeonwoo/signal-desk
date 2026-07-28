"""장중 DART lite poll — 공시→kb_events만, Sonnet/digest 없음."""

import time

from signal_desk import db, kb
from signal_desk.signals import decision as decmod


def test_poll_disclosures_creates_new_eligible(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    pub = "2026-07-28"
    ymd = "20260728"
    items = [{
        "title": "[공시] 감자 결정",
        "source": "dart",
        "published": pub,
        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={ymd}000888",
        "rcept_no": f"{ymd}000888",
        "doc_class": "공시",
    }]
    monkeypatch.setattr(kb, "_disclosure_items", lambda corp: items if corp == "00126380" else [])
    monkeypatch.setattr(kb, "corp_codes_cached", lambda **k: {"005930": "00126380"})

    out = kb.poll_disclosures([{"ticker": "005930", "name": "삼성전자"}])
    assert out["polled"] == 1
    assert out["synced"] == 1
    assert out["new_eligible"] == ["005930"]
    assert out["new_events"][0]["severity"] == "critical"

    # 재폴링 — 신규 아님
    out2 = kb.poll_disclosures([{"ticker": "005930", "name": "삼성전자"}])
    assert out2["new_eligible"] == []
    assert out2["synced"] == 1  # upsert는 다시 돌지만 new는 빈다


def test_poll_skips_us_tickers(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb, "corp_codes_cached", lambda **k: {"AAPL": "x"})
    called = []
    monkeypatch.setattr(kb, "_disclosure_items", lambda c: called.append(c) or [])
    out = kb.poll_disclosures([{"ticker": "AAPL", "name": "Apple"}])
    assert out["polled"] == 0
    assert called == []


def test_sentiment_map_includes_event_without_digest(tmp_path, monkeypatch):
    """lite poll만 돌린 종목도 Decision이 evaluate에 들어가야 한다."""
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = int(time.time())
    db.kb_event_upsert({
        "event_key": "dart:lite1",
        "ticker": "000660",
        "event_type": "disclosure_critical",
        "direction": "negative",
        "severity": "critical",
        "status": "confirmed",
        "decision_eligible": True,
        "decision_action": "exit",
        "detected_at": now,
        "expires_at": now + 86400,
        "summary": "상장폐지 — 테스트",
        "policy_version": "p0",
    }, evidence={"source_key": "dart", "url": "https://example.com/1", "trust_score": 1.0})
    assert db.kb_digest_get("000660") is None
    sm = kb.sentiment_map()
    assert "000660" in sm
    assert sm["000660"]["event_risk"] is True
    assert sm["000660"]["decision"].buy_blocked is True
    assert sm["000660"]["decision"].holding_action == "exit"


def test_corp_codes_cached_uses_kv(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    calls = {"n": 0}

    def fake_codes():
        calls["n"] += 1
        return {"005930": "00126380"}

    monkeypatch.setattr(kb.ingest_dart, "corp_codes", fake_codes)
    a = kb.corp_codes_cached(ttl_hours=24)
    b = kb.corp_codes_cached(ttl_hours=24)
    assert a == b == {"005930": "00126380"}
    assert calls["n"] == 1
