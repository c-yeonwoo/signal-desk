"""DART 주요공시 → 악재/호재 KB — 공시 파싱·필터·악재 감지(뉴스 오탐 없이)·refresh 병합."""

from signal_desk import kb
from signal_desk.ingest import dart


def test_dart_disclosures_parse(monkeypatch):
    monkeypatch.setattr(dart, "_get_json", lambda p, params: {"status": "000", "list": [
        {"report_nm": "유상증자 결정", "rcept_dt": "20260708", "rcept_no": "20260708000123"},
        {"report_nm": "  ", "rcept_dt": "20260707", "rcept_no": "z"}]})  # 빈 제목 제외
    out = dart.disclosures("00126380", "20260701", "20260708")
    assert len(out) == 1 and out[0]["report_nm"] == "유상증자 결정"
    monkeypatch.setattr(dart, "_get_json", lambda p, params: None)
    assert dart.disclosures("x", "a", "b") == []


def test_disclosure_items_filters_notable(monkeypatch):
    monkeypatch.setattr(kb.ingest_dart, "disclosures", lambda cc, b, e: [
        {"report_nm": "유상증자 결정", "rcept_dt": "20260708", "rcept_no": "1"},
        {"report_nm": "분기보고서", "rcept_dt": "20260707", "rcept_no": "2"},       # routine → 제외
        {"report_nm": "단일판매·공급계약 체결", "rcept_dt": "20260706", "rcept_no": "3"}])
    items = kb._disclosure_items("00126380")
    titles = [i["title"] for i in items]
    assert "[공시] 유상증자 결정" in titles and "[공시] 단일판매·공급계약 체결" in titles
    assert all("분기보고서" not in t for t in titles)                                 # routine 스킵
    assert all(i["source"] == "dart" and i["url"].startswith("https://dart.fss.or.kr") for i in items)
    assert kb._disclosure_items(None) == []                                          # 코드 없으면 []


def test_disclosure_event_but_news_not(monkeypatch):
    # 공시(source=dart) 유상증자 → serious veto. 같은 단어가 뉴스에 있어도 veto 안 함(오탐 방지).
    flag, note = kb.detect_event([{"title": "[공시] 유상증자 결정", "source": "dart"}])
    assert flag and kb.event_severity(note) == "serious"
    assert kb.detect_event([{"title": "유상증자 검토 보도", "source": "naver_news"}])[0] is False


def test_refresh_merges_disclosures(tmp_path, monkeypatch):
    monkeypatch.setattr(kb.db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [])          # 뉴스 없음
    monkeypatch.setattr(kb.ingest_dart, "corp_codes", lambda: {"005930": "00126380"})
    monkeypatch.setattr(kb.ingest_dart, "disclosures", lambda cc, b, e: [
        {"report_nm": "감자 결정", "rcept_dt": "20260708", "rcept_no": "9"}])
    monkeypatch.setattr(kb, "build_digest", lambda name, items: {"sentiment": -0.5, "summary": "s", "points": []})
    out = kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    assert out["updated"] == 1                                          # 뉴스 0이어도 공시로 갱신
    dg = kb.db.kb_digest_get("005930")
    assert dg["event_flag"] and "감자" in (dg["event_note"] or "")       # 공시 악재 veto 반영
