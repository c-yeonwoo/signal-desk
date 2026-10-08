"""3.0 company review: sourced explanation, never a new trading verdict."""

import datetime as dt
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from signal_desk import api, company_review, db, kb, kb_attribution


NOW = dt.datetime(2026, 10, 6, 8, 0, tzinfo=dt.timezone.utc)


def _review(**overrides):
    data = {
        "ticker": "005930", "market": "kr",
        "item": {"kind": "STRONG_BUY", "data_coverage": 0.94, "rank": 3},
        "price_date": "2026-10-06", "expected_date": "2026-10-06",
        "news": [], "official": [], "holding": None, "watching": False,
        "checked_at": int(NOW.timestamp()), "source_check_ok": True, "now": NOW,
        "provisional_at": None,
    }
    data.update(overrides)
    return company_review.build(**data)


def test_current_buy_and_official_risk_are_separate_claims_with_source_ids():
    event = {
        "id": 17, "status": "confirmed", "trust_tier": "official", "policy_version": "p0",
        "decision_eligible": True, "severity": "serious", "summary": "확인된 제재",
        "detected_at": int(NOW.timestamp()), "expires_at": int(NOW.timestamp()) + 86400,
        "evidence": [{"source_key": "dart", "url": "https://dart.fss.or.kr/filing/17",
                      "published": "2026-10-06T09:00:00+09:00"}],
    }
    review = _review(official=[event], holding={"ticker": "005930", "qty": 2})
    assert review["signal"]["decision"] == "STRONG_BUY"  # engine output unchanged
    assert review["signal"]["coverage"] == 0.94
    assert review["portfolio_relation"] == "held"
    assert [(c["relation"], c["evidence_ids"][0]) for c in review["claims"]] == [
        ("supports", "price:kr:005930:2026-10-06"), ("contradicts", "event:17")]
    assert review["evidence"][1]["url"] == "https://dart.fss.or.kr/filing/17"
    assert review["not_order_advice"] is True


def test_stale_prices_and_failed_news_check_do_not_launder_old_claims():
    old = {"id": 2, "title": "삼성전자, 지난주 실적", "url": "https://news.example/2",
           "published": "2026-09-29T09:00:00+09:00", "attribution_checked_at": int(NOW.timestamp())}
    review = _review(price_date="2026-10-02", news=[old], source_check_ok=False)
    assert review["price_status"] == "stale"
    assert review["signal"]["decision"] == "UNAVAILABLE"
    assert review["claims"] == []
    assert any("최근 뉴스" in reason for reason in review["unknowns"])


def test_intraday_quote_is_not_disguised_as_confirmed_close_and_expires():
    current = _review(provisional_at=NOW.timestamp() - 60)
    assert current["price_basis"] == "intraday_quote"
    assert current["signal"]["decision"] == "STRONG_BUY"
    assert current["evidence"][0]["observed_at"] is not None
    assert current["evidence"][0]["id"].startswith("price:kr:005930:live:")
    stale = _review(provisional_at=NOW.timestamp() - 900)
    assert stale["price_status"] == "stale"
    assert stale["signal"]["decision"] == "UNAVAILABLE"


def test_blocked_buy_is_not_shown_as_positive_candidate_claim():
    review = _review(item={"kind": "STRONG_BUY", "decision": {"buy_blocked": True}})
    assert review["signal"]["decision"] == "STRONG_BUY"
    assert review["buy_blocked"] is True
    assert not any(c["relation"] == "supports" for c in review["claims"])
    assert "차단" in review["claims"][0]["text"]


def test_news_context_deduplicates_reprints_and_rejects_unsafe_or_expired_sources():
    current = {"id": 3, "title": "삼성전자, 실적 발표", "url": "https://news.example/3",
               "published": "2026-10-06T09:00:00+09:00", "attribution_checked_at": int(NOW.timestamp())}
    rows = [current, {**current, "id": 4, "url": "https://news.example/reprint"},
            {**current, "id": 5, "title": "지난 소식", "published": "2026-09-20T09:00:00+09:00"},
            {**current, "id": 6, "title": "위험한 링크", "url": "javascript:alert(1)"}]
    review = _review(news=rows)
    assert [c["evidence_ids"] for c in review["claims"] if c["relation"] == "context"] == [["kb:3"]]
    assert all(e["url"].startswith("https://") for e in review["evidence"] if e["url"])
    assert review["signal"]["decision"] == "STRONG_BUY"


def test_expired_or_unreviewed_disclosure_cannot_be_presented_as_current_risk():
    event = {"id": 9, "status": "confirmed", "trust_tier": "official", "policy_version": "p0",
             "decision_eligible": True, "severity": "serious", "summary": "지난 위험",
             "expires_at": int(NOW.timestamp()) - 1,
             "evidence": [{"source_key": "dart", "url": "https://dart.fss.or.kr/old"}]}
    review = _review(official=[event, {**event, "id": 10, "status": "candidate",
                                       "expires_at": int(NOW.timestamp()) + 86400}])
    assert not any(e["kind"] == "official_filing" for e in review["evidence"])
    assert not any(c["relation"] == "contradicts" for c in review["claims"])


def test_detail_review_is_private_and_uses_only_own_manual_holdings(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(api, "_kr_signal_detail", lambda ticker: {
        "ticker": ticker, "name": "삼성전자", "kind": "HOLD", "data_coverage": 0.9, "rank": 10})
    monkeypatch.setattr(api.store, "load_price_history", lambda ticker: [{"date": "2026-10-06", "close": 100}])
    monkeypatch.setattr(api.store, "load_price_series", lambda: {"005930": [100]})
    monkeypatch.setattr(api.store, "load_signal_history", lambda: pd.DataFrame())
    monkeypatch.setattr(api.market_clock, "latest_completed_session", lambda market, now: "2026-10-06")
    monkeypatch.setattr(kb, "digest_checks", lambda: {"005930": {
        "status": "ok", "checked_at": int(dt.datetime.now(dt.timezone.utc).timestamp())}})
    db.kb_entry_add_many("005930", [{"title": "삼성전자, 실적 발표", "url": "https://news.example/earnings",
                                 "source": "naver_news", "published": dt.datetime.now(dt.timezone.utc).isoformat(),
                                 "attribution_version": kb_attribution.POLICY_VERSION}])
    first = TestClient(api.app)
    second = TestClient(api.app)
    assert first.get("/api/signals/005930/detail").status_code == 401
    assert first.post("/api/auth/signup", json={"email": "review-a@example.com", "pw": "abcdef"}).status_code == 200
    assert second.post("/api/auth/signup", json={"email": "review-b@example.com", "pw": "abcdef"}).status_code == 200
    assert first.post("/api/holdings", json={"ticker": "005930", "qty": 2, "avg_price": 100}).status_code == 200
    a = first.get("/api/signals/005930/detail")
    b = second.get("/api/signals/005930/detail")
    assert a.headers["cache-control"] == b.headers["cache-control"] == "private, no-store"
    assert a.json()["review"]["portfolio_relation"] == "held"
    assert b.json()["review"]["portfolio_relation"] == "not_in_portfolio"
    assert a.json()["review"]["signal"]["decision"] == "HOLD"
    assert {e["id"] for e in a.json()["review"]["evidence"]} == {
        "price:kr:005930:2026-10-06", "kb:1"}

    # 다른 종목이 방금 갱신돼 전역 시각만 새로워도 이 종목의 오래된 잠정가는 현재가가 아니다.
    monkeypatch.setattr(api.store, "load_price_series", lambda: {"005930": [100, 101]})
    old_quote = dt.datetime.now(dt.timezone.utc).timestamp() - 900
    monkeypatch.setattr(api.store, "live_quote_updated", lambda ticker: old_quote)
    monkeypatch.setattr(api.store, "live_status", lambda: {"updated": dt.datetime.now(dt.timezone.utc).timestamp()})
    stale = first.get("/api/signals/005930/detail").json()["review"]
    assert stale["price_status"] == "stale"
    assert stale["signal"]["decision"] == "UNAVAILABLE"
    monkeypatch.setattr(api.store, "live_quote_updated", lambda ticker: None)
    missing_time = first.get("/api/signals/005930/detail").json()["review"]
    assert missing_time["price_status"] == "stale"
    assert missing_time["signal"]["decision"] == "UNAVAILABLE"


def test_review_ui_escapes_sources_and_clears_previous_ticker():
    if not shutil.which("node"):
        pytest.skip("Node is needed for the detail renderer")
    script = r"""
const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
const html=fs.readFileSync('src/signal_desk/web/index.html','utf8');
const code=html.slice(html.indexOf('function _paintSignalReview('),html.indexOf('// 차트 표시 모드',html.indexOf('function _paintSignalReview(')));
const el={style:{},innerHTML:'',textContent:''};
const ctx=vm.createContext({document:{getElementById:()=>el},INDEX_TICKER:'INDEX',Map,Date,
  esc:s=>String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]))});
vm.runInContext(code,ctx);
ctx._paintSignalReview('005930',{ticker:'005930',price_status:'current',price_basis:'confirmed_close',
  as_of:{prices_through:'2026-10-06'},claims:[{relation:'context',text:'<img onerror=alert(1)>',evidence_ids:['kb:7']}],
  evidence:[{id:'kb:7',kind:'news',published_at:'2026-10-06T09:00:00+09:00',url:'javascript:alert(1)'}],
  holding_text:'분석용 보유 입력에는 없음',next_checks:['다음 자료 확인'],unknowns:[]});
assert.match(el.innerHTML,/&lt;img/);
assert.match(el.innerHTML,/기사 제목 ·/);
assert.match(el.innerHTML,/기사 내용의 사실 여부는 검증하지 않았어요/);
assert.doesNotMatch(el.innerHTML,/확인된 기업 소식/);
assert.doesNotMatch(el.innerHTML,/href="javascript:/);
ctx._paintSignalReview('AAPL',null);
assert.equal(el.textContent,'기업 근거와 내 보유를 함께 확인하는 중…');
"""
    result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
