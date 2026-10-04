"""Search snippets must not turn a counterparty's numbers into issuer facts."""

import datetime

from signal_desk import api, db, kb, kb_attribution, store


TODAY = datetime.date.today().isoformat()


def _item(title: str, summary: str = "", url: str = "https://news.example/story") -> dict:
    return {"title": title, "summary": summary, "url": url,
            "source": "naver_news", "published": TODAY}


def test_counterparty_cannot_inherit_supplier_contract_or_sales_ratio():
    item = _item("현대무벡스, 금호타이어와 720억원 공급계약 체결",
                 "현대무벡스의 매출액 대비 18.28%")
    rejected = kb_attribution.verify_news("금호타이어", item)
    assert rejected == {"ok": False, "reason": "제목의 주체 기업을 확인할 수 없음"}
    accepted = kb_attribution.verify_news("현대무벡스", item)
    assert accepted["ok"] is True and accepted["amount_basis"] == "현대무벡스"


def test_contract_role_and_denominator_are_required_even_for_lead_issuer():
    ambiguous = _item("금호타이어, 현대무벡스와 720억원 계약", "매출액 대비 18.28%")
    assert "역할" in kb_attribution.verify_news("금호타이어", ambiguous)["reason"]
    wrong_basis = _item("현대무벡스, 금호타이어와 720억원 공급계약 체결",
                        "금호타이어의 매출액 대비 18.28%")
    assert "기준 회사" in kb_attribution.verify_news("현대무벡스", wrong_basis)["reason"]
    valid = _item("현대무벡스, 금호타이어와 720억원 공급계약 체결",
                  "현대무벡스의 지난해 매출액 대비 18.28%")
    assert kb_attribution.verify_news("현대무벡스", valid)["ok"]
    mixed = _item("현대무벡스, 금호타이어와 720억원 공급계약 체결",
                  "현대무벡스의 매출액 대비 18%, 금호타이어의 매출액 대비 2%")
    assert "기준 회사" in kb_attribution.verify_news("현대무벡스", mixed)["reason"]
    buyer = _item("금호타이어, 현대무벡스로부터 장비 공급계약 체결",
                  "현대무벡스에 720억원 발주")
    assert "공급자인지" in kb_attribution.verify_news("금호타이어", buyer)["reason"]


def test_name_prefix_and_unsafe_url_fail_closed():
    assert not kb_attribution.verify_news("HD현대", _item("HD현대일렉트릭, 신규 수주"))["ok"]
    assert not kb_attribution.verify_news("현대차", _item("현대차증권, 실적 개선"))["ok"]
    assert not kb_attribution.verify_news("삼성전자", _item("삼성전자 협력사, 대형 공급계약"))["ok"]
    assert not kb_attribution.verify_news("삼성전자", _item("삼성전자, 실적 개선", url="javascript:alert(1)"))["ok"]


def test_misattributed_news_is_not_stored_or_summarized(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [
        _item("현대무벡스, 금호타이어와 720억원 공급계약 체결",
              "현대무벡스의 매출액 대비 18.28%")])
    monkeypatch.setattr(kb, "corp_codes_cached", lambda: {})
    assert kb.refresh([{"ticker": "073240", "name": "금호타이어"}])["updated"] == 0
    assert db.kb_entries_recent("073240") == []
    assert db.kb_digest_get("073240") is None


def test_verified_news_and_legacy_archive_are_separate_on_own_watchlist(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(api, "_uid", lambda _request: 7)
    monkeypatch.setattr(store, "load_universe", lambda: [{"ticker": "005930", "name": "삼성전자"}])
    monkeypatch.setattr(store, "load_us_universe", lambda: [])
    db.fav_add(7, "ticker", "005930", "삼성전자")
    db.fav_add(8, "ticker", "073240", "금호타이어")
    db.kb_document_add("005930", "옛날 기사", "legacy", "https://news.example/old",
                       "naver_news", TODAY, "뉴스")
    raw = _item("삼성전자, 실적 발표", "영업이익 개선", "https://news.example/verified")
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [raw])
    monkeypatch.setattr(kb, "corp_codes_cached", lambda: {})
    monkeypatch.setattr(kb, "build_digest", lambda name, items: {
        "sentiment": 0.1, "summary": "삼성전자 실적 발표", "points": []})
    kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    rows = api.watchlist_briefs_get(object())["items"]
    assert len(rows) == 1 and rows[0]["ticker"] == "005930"
    assert [e["url"] for e in rows[0]["evidence"]] == [raw["url"]]
    assert rows[0]["evidence"][0]["checked_at"] is not None
    assert rows[0]["not_order_advice"] is True


def test_existing_url_of_other_issuer_cannot_enter_second_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    raw = _item("삼성전자, 실적 발표")
    db.kb_document_add("073240", "이전 오귀속", "", raw["url"], "naver_news", TODAY, "뉴스")
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [raw])
    monkeypatch.setattr(kb, "corp_codes_cached", lambda: {})
    assert kb.refresh([{"ticker": "005930", "name": "삼성전자"}])["updated"] == 0
    assert db.kb_digest_get("005930") is None


def test_dart_response_issuer_code_must_match_requested_company(monkeypatch):
    monkeypatch.setattr(kb.ingest_dart, "disclosures", lambda *a, **k: [
        {"report_nm": "유상증자 결정", "rcept_dt": "20261004", "rcept_no": "20261004000001",
         "corp_code": "99999999"}])
    assert kb._disclosure_items("00126380") == []


def test_watchlist_tab_and_storage_diagnostic_are_visible():
    html = (api.WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'data-mkt="watchlist"' in html and 'id="watchlist-briefs"' in html
    assert "최근 변화" in html and "아직 모르는 점" in html
    assert "마지막 수집 시도" in html and "수집 확인 기록 없음" in html
    assert "뉴스 분위기는 점수나 매매 차단에 쓰지 않습니다" in html
    assert "sto.data_bytes" in html and "sto.largest_paths" in html
