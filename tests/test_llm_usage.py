"""이 앱 LLM 사용량·추정 비용 집계."""

from signal_desk import db, llm


def test_price_and_estimate():
    assert llm.price_for_model("claude-opus-4-8") == (5.0, 25.0)
    assert llm.price_for_model("claude-sonnet-5") == (2.0, 10.0)
    assert llm.price_for_model("claude-haiku-4-5-20251001") == (1.0, 5.0)
    # 1M in + 1M out opus → $30
    assert abs(llm.estimate_cost_usd("claude-opus-4-8", 1_000_000, 1_000_000) - 30.0) < 1e-9
    # 1k in haiku → $0.001
    assert abs(llm.estimate_cost_usd("claude-haiku-4-5-20251001", 1000, 0) - 0.001) < 1e-9


def test_record_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    llm._record_usage("claude-sonnet-5", {"input_tokens": 1000, "output_tokens": 500}, kind="complete")
    llm._record_usage("claude-haiku-4-5-20251001", {"input_tokens": 2000, "output_tokens": 100}, kind="complete")
    s = db.llm_usage_summary(days=30)
    assert s["total"]["calls"] == 2
    assert s["total"]["input_tokens"] == 3000
    assert s["total"]["cost_usd"] > 0
    models = {m["model"]: m for m in s["by_model"]}
    assert "claude-sonnet-5" in models
    assert models["claude-sonnet-5"]["calls"] == 1


def test_record_skips_empty_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    llm._record_usage("claude-opus-4-8", None, kind="complete")
    llm._record_usage("claude-opus-4-8", {"input_tokens": 0, "output_tokens": 0}, kind="complete")
    assert db.llm_usage_summary()["total"]["calls"] == 0
