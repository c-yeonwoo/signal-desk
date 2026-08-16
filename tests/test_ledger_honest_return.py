"""장부 성과에 실현손익 분해와 벤치마크가 없었다(2026-08-16).

두 가지가 빠져 있었다.

**① 실현손익이 어디에도 없다.** `paper.balance` 의 `pnl` 은 **지금 들고 있는 것**의 평가손익
뿐이라, 손실을 확정하고 팔면 그 손실이 통째로 사라진다. 실측(균형형): 평가손익 +83,200원인데
실현손익은 **−431,200원**이었고 계좌는 −3.48%였다. 화면 라벨(`보유 손익률 · 들고 있는 것만`)은
정직했지만 **판 종목의 손실은 어느 화면에도 없었다** — 리셋 버튼 없이도 장부가 좋아 보이는
경로이고, 백테스트에서 그렇게 경계하는 생존편향과 같은 병이다.

**② 기준선이 없다.** 공격형 `+1.58%` 가 잘한 것인지 못한 것인지 말할 수 없었다. 같은 28일
동일가중 유니버스는 **+1.89%** 라 셋 다 미달이었다:

    안정형 −0.43% → 초과 −2.32%p
    균형형 −3.48% → 초과 −5.37%p
    공격형 +1.58% → 초과 −0.31%p

이 리포의 1번 지표 규칙(*base rate 없는 비율은 노출 금지 · 기준선과 리프트를 항상 함께*)이
장부에는 적용돼 있지 않았다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from signal_desk import bot

_HTML = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web" / "index.html"


def _bal(cash, stock_eval, invested):
    pnl = stock_eval - invested
    return {"cash": cash, "stock_eval": stock_eval, "invested": invested, "pnl": pnl,
            "pnl_pct": round(pnl / invested * 100, 2) if invested else None,
            "total_eval": cash + stock_eval, "holdings": []}


def test_realized_loss_is_not_hidden_by_selling():
    """**이게 그 버그다.** 손절하면 손실이 `pnl` 에서 사라지고 남은 승자가 비율을 올린다."""
    # 실측 균형형: 시드 1,000만 · 현금 8,123,100 · 평가 1,528,900 · 매입 1,445,700
    r = bot._return_block(_bal(8_123_100, 1_528_900, 1_445_700), 10_000_000)
    assert r["unrealized_pnl"] == 83_200, "보유분 평가손익"
    assert r["realized_pnl"] == -431_200, "판 종목의 손실이 안 보인다"
    assert r["total_pnl"] == -348_000
    assert r["total_return_pct"] == pytest.approx(-3.48)


def test_decomposition_always_adds_up():
    """실현 + 평가 = 총손익. 안 맞으면 어느 쪽이 새는지 모른다."""
    for cash, se, inv, seed in [(5e6, 6e6, 5e6, 10e6), (12e6, 0, 0, 10e6), (0, 9e6, 11e6, 10e6)]:
        r = bot._return_block(_bal(cash, se, inv), seed)
        assert r["realized_pnl"] + r["unrealized_pnl"] == pytest.approx(r["total_pnl"])


def test_no_seed_means_no_verdict_not_zero():
    """시드를 모르면 **모른다고 한다** — 0으로 채우면 전액 손실처럼 보인다."""
    r = bot._return_block(_bal(1e6, 1e6, 1e6), 0)
    assert r["total_return_pct"] is None and r["total_pnl"] is None


def test_ledger_state_exposes_the_decomposition(monkeypatch):
    """API가 안 내면 화면이 만들 수 없다."""
    monkeypatch.setattr(bot, "_cfg", lambda uid: {"enabled": True, "seed_cash": 10_000_000,
                                                  "seed_cash_us": 10_000, "trading_style": "balanced"})
    monkeypatch.setattr(bot.paper, "balance", lambda uid, m: _bal(8_123_100, 1_528_900, 1_445_700))
    monkeypatch.setattr(bot, "reconcile_positions", lambda *a, **k: None)
    monkeypatch.setattr(bot.db, "bot_positions_all", lambda *a, **k: [])
    monkeypatch.setattr(bot.db, "bot_trades_recent", lambda *a, **k: [])
    monkeypatch.setattr(bot.db, "bot_reservations_pending", lambda *a, **k: [])
    st = bot._state(1, "kr")
    assert st["total_return_pct"] == pytest.approx(-3.48)
    assert st["realized_pnl"] == -431_200
    assert st["pnl_pct"] is not None, "보유 손익률은 보조 지표로 남는다(없애지 않는다)"


def test_benchmark_uses_the_same_window_and_population(monkeypatch):
    """지평·기간·모집단이 하나라도 다르면 리프트는 거짓이다."""
    monkeypatch.setattr(bot.store, "load_universe",
                        lambda: [{"ticker": "A"}, {"ticker": "B"}, {"ticker": "OUT"}])
    monkeypatch.setattr(bot.store, "load_all_dated_closes", lambda: {
        "A": (["2026-07-08", "2026-08-14"], [100.0, 110.0]),      # +10%
        "B": (["2026-07-08", "2026-08-14"], [100.0, 90.0]),       # −10%
        "OUT": (["2026-07-08", "2026-08-14"], [100.0, 500.0]),    # 유니버스 밖이면 안 섞인다
        "GHOST": (["2026-07-08", "2026-08-14"], [100.0, 900.0]),  # 유니버스에 없으면 무시
    })
    curve = [{"date": "2026-07-08"}, {"date": "2026-08-14"}]
    assert bot.benchmark_return_pct(curve, "kr") == pytest.approx((10 - 10 + 400) / 3, abs=0.01)


def test_benchmark_is_none_when_it_cannot_be_built(monkeypatch):
    """**추측하지 않는다.** 0%를 끼워 넣으면 초과수익이 곧 수익률이 된다."""
    monkeypatch.setattr(bot.store, "load_universe", lambda: [])
    monkeypatch.setattr(bot.store, "load_all_dated_closes", lambda: {})
    assert bot.benchmark_return_pct([{"date": "a"}, {"date": "b"}], "kr") is None
    assert bot.benchmark_return_pct([{"date": "a"}], "kr") is None, "한 점으로는 기간이 없다"


def test_benchmark_survives_a_broken_price_cache(monkeypatch):
    """장부는 계속 보여야 한다 — 벤치마크가 못 만들어져도 수익률은 나온다."""
    def boom():
        raise RuntimeError("parquet 손상")
    monkeypatch.setattr(bot.store, "load_all_dated_closes", boom)
    monkeypatch.setattr(bot.store, "load_universe", lambda: [{"ticker": "A"}])
    assert bot.benchmark_return_pct([{"date": "a"}, {"date": "b"}], "kr") is None


def test_screen_shows_excess_return_not_just_the_raw_number():
    """기준선 없는 수익률은 잘한 것인지 말해주지 않는다 — 화면에 있어야 한다."""
    src = re.sub(r"^\s*//.*$", "", _HTML.read_text(encoding="utf-8"), flags=re.M)
    assert "excess_return_pct" in src, "초과수익이 화면에 없다"
    assert src.count("excess_return_pct") >= 2, "레퍼런스 표와 자산곡선 요약 양쪽에 있어야 한다"
    assert "realized_pnl" in src, "실현손익이 화면에 없다 — 판 종목의 손실이 사라진다"
