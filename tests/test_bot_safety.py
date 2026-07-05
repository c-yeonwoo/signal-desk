"""자동매매 안전장치 — kill switch·일일 손실한도(유저별)."""

import json

from signal_desk import bot, db

UID = 4


def test_kill_switch_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_KILL_SWITCH", "true")
    out = bot.run_once(UID)
    assert out["ok"] is False and "긴급정지" in out["reason"]


def test_daily_loss_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_DAILY_LOSS_LIMIT_PCT", "0.08")
    # 최초: 기준선 기록만(미초과)
    assert bot._daily_loss_breached(UID, {"total_eval": 1_000_000.0}, dry_run=False) is False
    assert bot._daily_loss_breached(UID, {"total_eval": 970_000.0}, dry_run=False) is False   # -3% 미초과
    assert bot._daily_loss_breached(UID, {"total_eval": 900_000.0}, dry_run=False) is True     # -10% 초과


def test_daily_loss_limit_isolated_per_uid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bot._daily_loss_breached(UID, {"total_eval": 1_000_000.0}, dry_run=False)  # UID 기준선만 기록
    # 다른 유저는 자기 기준선이 없으므로 처음엔 미초과(기록만)
    assert bot._daily_loss_breached(99, {"total_eval": 500_000.0}, dry_run=False) is False
