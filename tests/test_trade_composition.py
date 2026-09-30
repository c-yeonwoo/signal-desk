"""장부가 최근 N건만 보여 주면 추가매수가 전략처럼 보이고, 청산 규칙 변경 전이 평균을 지배한다."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from signal_desk import bot, db
from signal_desk.signals import roadmap_status

_HTML = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web" / "index.html"
DAY = 86400


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data/cache").mkdir(parents=True)
    db._CONN = None
    return tmp_path


def _at(ts):
    c = db.conn()
    c.execute("UPDATE bot_trades SET ts=? WHERE id=(SELECT MAX(id) FROM bot_trades)", (int(ts),))
    c.commit()
    c.close()


def _trade(side, reason, ts, qty=1):
    db.bot_trade_log(1, "005930", "삼성전자", side, qty, 70000, reason, None, market="kr")
    _at(ts)


def test_composition_counts_the_whole_book_not_the_recent_page(fresh):
    base = 1_700_000_000
    _trade("buy", "SIGNAL", base)
    _trade("buy", "ADD", base + 10)
    _trade("buy", "ADD", base + 20)
    _trade("sell", "TRAILING", base + 30)
    comp = bot.trade_composition(1, "kr")
    assert comp["all"]["n"] == 4
    assert comp["all"]["buys"] == {"SIGNAL": 1, "ADD": 2}
    assert comp["all"]["sells"] == {"TRAILING": 1}


def test_exit_policy_split_uses_the_market_date(fresh):
    """2026-09-07 00:00 KST 이후만 새 청산 규칙이다. UTC 날짜로 자르면 하루가 밀린다."""
    start = dt.datetime(2026, 9, 7, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
    before = (start - dt.timedelta(hours=1)).timestamp()
    after = (start + dt.timedelta(hours=1)).timestamp()
    _trade("buy", "SIGNAL", before)
    _trade("sell", "TRAILING", before + 10)
    _trade("buy", "ADD", after)
    _trade("sell", "STOP_LOSS", after + 10)
    since = bot.trade_composition(1, "kr")["since_exit_policy"]
    assert since["buys"] == {"ADD": 1}
    assert since["sells"] == {"STOP_LOSS": 1}
    assert "TRAILING" not in since["sells"]


def test_holding_after_the_policy_keeps_fifo_but_drops_old_closes(fresh):
    start = dt.datetime(2026, 9, 7, tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp()
    _trade("buy", "SIGNAL", start - 6 * DAY, qty=10)
    _trade("sell", "TRAILING", start - 1 * DAY, qty=10)   # 옛 규칙에서 청산 — 창 밖
    _trade("buy", "SIGNAL", start, qty=10)
    _trade("sell", "STOP_LOSS", start + 2 * DAY, qty=10)
    st = bot.holding_period_stats(1, "kr", closed_on_or_after=bot.EXIT_POLICY_SESSION)
    assert st["closed_lots"] == 1
    assert st["median_days"] == 2.0


def test_no_close_after_the_policy_says_why(fresh):
    start = dt.datetime(2026, 9, 6, 10, tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp()
    _trade("buy", "SIGNAL", start, qty=1)
    _trade("sell", "TRAILING", start + 4 * 3600, qty=1)
    st = bot.holding_period_stats(1, "kr", closed_on_or_after="2026-09-07")
    assert st["median_days"] is None
    assert "이후 청산된 로트 없음" in st["reason"]


def test_roadmap_does_not_carry_a_return(fresh):
    out = roadmap_status.for_market("kr")
    blob = json.dumps(out)
    assert out["live_eligible"] is False
    assert [s["id"] for s in out["steps"]] == ["r11", "r12", "verdict", "later"]
    assert all(s["live_eligible"] is False for s in out["steps"])
    assert "net_delta" not in blob and "return_pct" not in blob
    assert out["champion"]["frozen"] is True
    assert out["steps"][0]["observed_sessions"] == 0
    assert "관측 0세션" in out["steps"][0]["reason"]


def test_roadmap_counts_same_plan_observations_without_a_return(fresh):
    from signal_desk.signals import rotation_shadow
    payload = {
        "version": rotation_shadow.VERSION,
        "decisions": {
            "champion_rotation_proxy": {"fixed_orders": []},
            "s0_rank_buffer": {"fixed_orders": []},
        },
    }
    for uid in (900001, 900002, 900003):
        assert db.rotation_shadow_add_once(uid, "kr", "2026-09-28", payload)
    out = roadmap_status.for_market("kr")
    step = out["steps"][0]
    assert step["observed_sessions"] == 1
    assert step["matured_blocks"] == 0
    assert "관측 1세션" in step["reason"]
    assert "갈라진" in step["reason"]
    assert "net_delta" not in json.dumps(out)


def test_screen_shows_the_mix_and_the_order_without_a_research_return():
    src = _HTML.read_text(encoding="utf-8")
    assert "holding_since_exit_policy" in src
    assert "_mixLine" in src and "_roadmapNote" in src
    assert "연구 수익률은 여기에 싣지 않습니다" in src
