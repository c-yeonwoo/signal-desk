"""Cloud evidence adaptation: source freshness, grounded quotes and zero extra calls."""

import datetime as dt
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from signal_desk import api, db, kb, kb_attribution, research_brief as research
from fastapi import Response

NOW = dt.datetime(2026, 10, 31, 2, tzinfo=dt.timezone.utc).timestamp()


def _item(**kwargs):
    return {"title": "삼성전자, 분기 실적 발표", "summary": "매출은 늘었지만 영업이익률은 낮아졌다",
            "url": "https://news.example/results", "source": "naver_news",
            "published": dt.datetime.fromtimestamp(NOW - 60, dt.timezone.utc).isoformat(), **kwargs}


def _capture(items, **kwargs):
    rows = research.sources(items, now=NOW)
    return research.capture({"supports": [{"source_id": rows[0]["id"], "quote": rows[0]["title"]}],
                             "risks": [{"source_id": rows[0]["id"], "quote": rows[0]["summary"]}],
                             "next_checks": ["earnings_basis"]}, rows, now=NOW, model="test-model") | kwargs


def _build(items, cached, **kwargs):
    return research.build(ticker="005930", items=items, cached=cached,
                          check={"status": "ok", "checked_at": NOW}, now=NOW, **kwargs)


def test_grounding_preserves_both_sides_but_not_a_trade_or_earnings_surprise():
    items = [_item()]
    result = _build(items, _capture(items))
    assert result["status"] == "grounded_extracts"
    assert result["supports"][0]["quote"] == items[0]["title"]
    assert result["risks"][0]["quote"] == items[0]["summary"]
    assert result["mode"] == "explanation_only" and result["not_order_advice"]
    assert result["profitability_verified"] is False
    assert any("예상치" in s for s in result["unknowns"])
    assert any("비용" in s for s in result["unknowns"])


def test_unknown_or_fabricated_quotes_and_model_commands_are_discarded():
    rows = research.sources([_item()], now=NOW)
    result = research.validate({
        "supports": [{"source_id": rows[0]["id"], "quote": "예상을 웃돈 실적, 지금 매수"}],
        "risks": [{"source_id": "other-company", "quote": rows[0]["summary"]}],
        "next_checks": ["BUY", "earnings_basis", "earnings_basis", {"command": "trade"}],
    }, rows)
    assert result == {"supports": [], "risks": [], "next_checks": ["earnings_basis"]}


@pytest.mark.parametrize("changed", [
    {"summary": "정정된 실적 원문으로 변경"},
    {"published": "2026-10-20T10:00:00+00:00"},
    {"published": "2026-11-01T10:00:00+00:00"},
    {"url": "javascript:alert(1)"},
])
def test_changed_old_future_or_unsafe_sources_cannot_keep_cached_analysis(changed):
    original = [_item()]
    result = _build([_item(**changed)], _capture(original))
    assert result["supports"] == result["risks"] == []
    assert result["model"] is None


def test_every_source_expires_even_when_another_source_is_recent():
    old = _item(url="https://news.example/earlier", published="2026-10-28T02:00:30+00:00")
    items = [old, _item()]
    cached = _capture(items)
    rows = research.sources(items, now=NOW)
    old_source = next(r for r in rows if r["url"] == old["url"])
    cached["supports"] = [{"source_id": old_source["id"], "quote": old["title"]}]
    result = research.build(ticker="005930", items=items, cached=cached,
                            check={"status": "ok", "checked_at": NOW}, now=NOW + 60)
    assert len(result["sources"]) == 1
    assert result["supports"] == result["risks"] == []


def test_failed_or_expired_collection_does_not_launder_a_recent_summary():
    items = [_item()]
    for check in ({"status": "failed", "checked_at": NOW},
                  {"status": "ok", "checked_at": NOW - 4 * 86400}):
        out = research.build(ticker="005930", items=items, cached=_capture(items), check=check, now=NOW)
        assert not out["sources"] and not out["supports"] and not out["risks"]


def test_date_only_dart_uses_korean_date_independently_of_server_timezone():
    original = os.environ.get("TZ")
    try:
        for timezone in ("UTC", "Asia/Seoul"):
            os.environ["TZ"] = timezone
            time.tzset()
            rows = research.sources([_item(source="dart", published="2026-10-29")], now=NOW)
            assert rows[0]["published"] == "2026-10-29T00:00:00+09:00"
            assert rows[0]["time_precision"] == "date_only"
            assert not research.sources([_item(source="dart", published="2026-10-29")],
                                        now=dt.datetime(2026, 10, 31, 23, tzinfo=dt.timezone.utc).timestamp())
            assert not research.sources([_item(published="2026-10-31T00:00:00")], now=NOW)
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_existing_refresh_produces_brief_in_one_call_and_reads_without_a_key(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    item = _item(published=now, attribution_version=kb_attribution.POLICY_VERSION)
    monkeypatch.setattr(kb, "corp_codes_cached", lambda: {})
    monkeypatch.setattr(kb, "_disclosure_items", lambda *a: [])
    monkeypatch.setattr(kb.news, "collect", lambda *a, **k: [item])
    monkeypatch.setattr(kb, "sync_candidate_events", lambda *a: 0)
    monkeypatch.setattr(kb.llm, "available", lambda: True)
    calls = []

    def complete(system, user, **kwargs):
        calls.append(kwargs)
        rows = research.sources([item], now=dt.datetime.now(dt.timezone.utc).timestamp())
        assert rows[0]["id"] in user and "기간·단위" in system
        return {"sentiment": .1, "summary": "분기 실적 발표", "points": [],
                "research": {"supports": [], "risks": [{"source_id": rows[0]["id"],
                                                         "quote": item["summary"]}],
                             "next_checks": ["earnings_basis"]}}

    monkeypatch.setattr(kb.llm, "complete_json", complete)
    assert kb.refresh([{"ticker": "005930", "name": "삼성전자"}])["updated"] == 1
    assert kb.refresh([{"ticker": "005930", "name": "삼성전자"}])["updated"] == 0
    assert len(calls) == 1 and calls[0]["model"] == kb.llm.DIGEST_MODEL
    assert calls[0]["purpose"] == "kb"
    monkeypatch.setattr(kb.llm, "available", lambda: False)
    report = api.kb_get("005930")["research"]
    assert report["status"] == "grounded_extracts" and report["risks"]
    # No research data reaches the existing trading summary/advisor input.
    assert "research" not in kb.advisor_digest("005930")
    assert "research" not in kb.sentiment_map()["005930"]
    # The real DB deliberately retains original same-URL text. A re-observed
    # correction must quarantine it, rather than certifying that old content again.
    item["summary"] = "정정: 매출과 영업이익이 모두 감소했다"
    kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    changed = api.kb_get("005930")["research"]
    assert changed["status"] == "source_changed" and not changed["sources"]
    assert not changed["supports"] and not changed["risks"]
    assert len(calls) == 1
    assert api.kb_get("005930")["digest"] is None
    assert api.kb_get("005930")["archived_digest"]["summary"] == "분기 실적 발표"
    assert "정정" not in db.kb_entries_recent("005930")[0]["summary"]
    db.kv_set("company_research_changes:005930", {})
    item["title"] = "삼성전자 협력사, 분기 실적 발표"
    kb.refresh([{"ticker": "005930", "name": "삼성전자"}])
    reattributed = api.kb_get("005930")["research"]
    assert reattributed["status"] == "source_changed" and not reattributed["sources"]
    assert len(calls) == 1  # Rejected raw re-observation still retracts old research.
    db.kv_set("kb_digest_checks", {"005930": {"status": "failed", "checked_at": 1}})
    assert not api.kb_get("005930")["research"]["sources"]


def test_daily_cloud_hook_runs_once_without_a_model_or_external_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(api, "_kb_targets", lambda: [{"ticker": "005930"}, {"ticker": "005930"}])
    def forbidden(*a, **k):
        pytest.fail("daily research must not call sources or models")
    monkeypatch.setattr(kb.llm, "complete_json", forbidden)
    monkeypatch.setattr(kb.news, "collect", forbidden)
    monkeypatch.setattr(kb, "corp_codes_cached", forbidden)
    monkeypatch.setattr(api.config, "kb_auto_collect", lambda: False)
    api._daily_company_research("2026-10-31")
    report = db.kv_get("company_research_refresh_last")
    assert report["targets"] == 1 and report["counts"] == {"check_required": 1}
    assert report["llm_calls"] == report["source_requests"] == 0
    monkeypatch.setattr(kb, "refresh_research_briefs", forbidden)
    api._daily_company_research("2026-10-31")


def test_detail_does_not_show_legacy_positive_summary_below_a_retraction(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    monkeypatch.setattr(api, "_kr_signal_detail", lambda ticker: {
        "ticker": ticker, "kind": "HOLD", "kb": {"summary": "옛 긍정 요약"}, "moves": ["옛 사실"]})
    monkeypatch.setattr(api, "_annotate_signal_price_status", lambda *a: None)
    monkeypatch.setattr(api, "_uid", lambda request: None)
    monkeypatch.setattr(api.store, "load_signal_history", lambda: SimpleNamespace(empty=True))
    monkeypatch.setattr(kb, "research_brief_get", lambda *a, **k: {
        "withheld_changed_sources": True, "status": "source_changed", "sources": []})
    out = api.signal_detail_get("005930", object(), Response())
    assert out["item"]["kb"] is out["item"]["moves"] is None
    assert out["item"]["kb_status"] == {"status": "source_changed", "current": False}
    assert out["item"]["kind"] == "HOLD"


def test_research_ui_escapes_quotes_and_keeps_official_risk_and_price_state():
    if not shutil.which("node"):
        pytest.skip("Node required for renderer")
    html = Path("src/signal_desk/web/index.html").read_text()
    start = html.index("function _companyResearchHtml(")
    code = html[start:html.index("// 차트 표시 모드", start)]
    script = r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const el={style:{},innerHTML:''};
const ctx=vm.createContext({document:{getElementById:()=>el},INDEX_TICKER:'INDEX',
esc:s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))});
vm.runInContext(CODE,ctx);
ctx._paintSignalReview('X',{ticker:'X',price_status:'stale',buy_blocked:true,
 claims:[{relation:'contradicts',text:'공식 위험 유지',evidence_ids:['event:1']}],evidence:[],
 research:{sources:[{id:'s',url:'https://news.example',title:'<script>x</script>',published:'2026-10-31'}],
 supports:[{source_id:'s',quote:'<img onerror=x>'}],risks:[],next_checks:['다음 보고서 확인']}});
assert.match(el.innerHTML,/공식 위험 유지/);
assert.match(el.innerHTML,/시세 기준을 확인할 수 없고 공식 위험도/);
assert.match(el.innerHTML,/&lt;img/); assert.doesNotMatch(el.innerHTML,/<img/);
assert.match(el.innerHTML,/가격 판정 근거 보기/);
assert.match(el.innerHTML,/다음 보고서 확인/);
assert.doesNotMatch(ctx._companyResearchHtml({sources:[],status:'source_failed'}),/이전 분석은 아직 유효/);
assert.match(ctx._companyResearchHtml({sources:[],status:'source_failed'}),/수집이 실패/);
'''.replace("CODE", json.dumps(code))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
