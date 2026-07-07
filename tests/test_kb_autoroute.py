"""KB 파일 업로드 자동 라우팅 — ticker 없이 종목/시황/섹터 분류 + 검증 안전망."""

from signal_desk import kb


def _setup(monkeypatch, tmp_path, scope, ticker=None, name=None, sector=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(kb, "_pdf_text", lambda data: "국내 증시 시황 및 반도체 업황 분석 " * 30)
    monkeypatch.setattr(kb, "_summarize_text", lambda n, t, x: ("요약", []))
    monkeypatch.setattr(kb, "_classify_scope",
                        lambda text: {"scope": scope, "ticker": ticker, "name": name, "sector": sector})
    monkeypatch.setattr(kb, "validate_macro", lambda text, title="": {"verdict": "accept", "reasons": []})
    monkeypatch.setattr(kb, "validate_import", lambda *a, **k: {"verdict": "accept", "trust": 0.9, "reasons": []})


def test_autoroute_market(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, "market")
    out = kb.import_file(None, "", "시황.pdf", b"%PDF...", "application/pdf")
    assert out["ok"] and out["routed"] == "market"
    docs = kb.db.kb_documents(ticker=kb.MACRO_TICKER)
    assert docs and docs[0]["title"].startswith("[시황]")


def test_autoroute_sector(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, "sector", sector="반도체")
    out = kb.import_file(None, "", "섹터.pdf", b"%PDF...", "application/pdf")
    assert out["ok"] and out["routed"] == "sector" and out["sector"] == "반도체"
    assert kb.db.kb_documents(ticker=kb.MACRO_TICKER)[0]["title"].startswith("[섹터: 반도체]")


def test_autoroute_stock(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, "stock", ticker="005930", name="삼성전자")
    out = kb.import_file(None, "", "삼성.pdf", b"%PDF...", "application/pdf")
    assert out["ok"] and out["routed"] == "stock" and out["ticker"] == "005930"
    assert kb.db.kb_documents(ticker="005930")


def test_autoroute_macro_reject_blocks(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path, "market")
    monkeypatch.setattr(kb, "validate_macro", lambda text, title="": {"verdict": "reject", "reasons": ["광고"]})
    out = kb.import_file(None, "", "ad.pdf", b"%PDF...", "application/pdf")
    assert out["ok"] is False and out["verdict"] == "reject"      # 안전망 작동
    assert kb.db.kb_documents(ticker=kb.MACRO_TICKER) == []
