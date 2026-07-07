"""공용 레퍼런스 봇 — 성향별 시스템 계정 부트스트랩 + 공개 track record."""

from signal_desk import bot, db


def test_ensure_reference_bots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bot.ensure_reference_bots()
    enabled = set(db.user_bots_enabled())
    for uid, style in bot.REFERENCE_BOTS.items():
        assert uid in enabled                              # 활성화됨(루프가 운용)
        assert db.user_bot_get(uid)["trading_style"] == style
    # 멱등 — 다시 불러도 중복/오류 없음
    bot.ensure_reference_bots()
    assert set(bot.REFERENCE_BOTS) <= set(db.user_bots_enabled())


def test_reference_performance_shape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = bot.reference_performance("kr")
    assert out["market"] == "kr" and len(out["bots"]) == 3
    styles = {b["style"] for b in out["bots"]}
    assert styles == {"conservative", "balanced", "aggressive"}
    b0 = out["bots"][0]
    assert "return_pct" in b0 and "curve" in b0 and "max_drawdown_pct" in b0
