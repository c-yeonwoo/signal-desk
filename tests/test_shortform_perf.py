"""성과 숏폼 — 레퍼런스 봇 track record를 숏폼 초안으로(봇↔숏폼 시너지)."""

from signal_desk import shortform, db, bot


def test_generate_performance_creates_draft(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, "performance", lambda uid, market="kr": {
        "market": "kr", "currency": "KRW", "seed": 10_000_000, "total_eval": 11_200_000,
        "return_pct": 12.0, "max_drawdown_pct": -4.5, "days": 30, "n_trades": 18, "n_sells": 9, "curve": []})
    out = shortform.generate_performance(style="balanced")
    assert out["ok"] and out["count"] == 1
    it = db.shortform_get(out["created"][0]["id"])
    assert it["kind"] == "PERF" and it["card_svg"].startswith("<svg")
    assert "12.0%" in it["title"] and "모의투자" in it["caption"]   # 면책 포함


def test_generate_performance_no_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot, "performance", lambda uid, market="kr": {"days": 0, "return_pct": None, "n_trades": 0})
    out = shortform.generate_performance(style="aggressive")
    assert out["ok"] is False and out["count"] == 0
