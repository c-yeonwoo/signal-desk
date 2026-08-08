"""빠른 틱(5분)은 **돈을 쓰지 않는다** + 루프는 프로세스당 하나.

2026-08-08 실측 배경:
- 봇 주기는 30분이었고, 통째로 줄이면 `advisor`(Opus)·`about/moves`(Haiku)가 배수로 늘어난다.
- 반면 손절·트레일링·예약은 **가격에 반응**하므로 자주 볼수록 실익이 있고 비용은 0이다.
- 첫 화면 21개 호출이 **동시성 1**에 줄을 서는데(병렬 4.4초 > 순차 3.0초), 워커를 늘리면
  매매 루프도 같이 늘어 **같은 종목을 두 번 산다.** 그래서 락이 성능 작업의 전제다.
"""

from __future__ import annotations

import inspect

from signal_desk import api, bot


# ─────────────────────────── 빠른 틱에 LLM이 없다 ───────────────────────────

def test_fast_tick_never_reaches_the_paid_buy_path():
    """빠른 틱은 `sells_only=True` 로만 부른다 — 매수(=advisor·Opus)에 닿으면 안 된다."""
    src = inspect.getsource(api._fast_trade_pass)
    assert "sells_only=True" in src, "빠른 틱이 전체 사이클을 돈다"
    assert "advisor" not in src and "_backfill_about" not in src and "_backfill_moves" not in src, \
        "빠른 틱에 유료 경로가 섞였다"
    # 시세 갱신 **뒤에** 와야 한다 — 순서가 반대면 낡은 시세로 손절을 판정한다.
    q = inspect.getsource(api._quote_loop_iteration)
    assert q.index("_refresh_live_quotes") < q.index("_fast_trade_pass"), \
        "매매 점검이 시세 갱신보다 먼저다 — 낡은 가격으로 판정한다"


def test_sells_only_zeroes_the_buy_slots(monkeypatch):
    """`sells_only` 는 **매수 자리를 0으로** 만든다. `if slots > 0:` 가 pool 구성·advisor를
    통째로 감싸므로, 0이면 LLM은 물론 KB 조회도 일어나지 않는다."""
    src = inspect.getsource(bot.run_once)
    assert "sells_only" in src
    # slots 계산에 sells_only 가 들어가는지 — 별도 분기를 만들면 반환 모양이 갈라진다.
    line = next(ln for ln in src.split("\n") if ln.strip().startswith("slots ="))
    assert "sells_only" in line, "sells_only 가 매수 자리 계산에 반영되지 않는다"
    assert "if slots > 0:" in src, "매수 블록이 slots 로 감싸여 있지 않다"


def test_sells_only_keeps_the_same_return_shape():
    """빠른 틱과 느린 틱이 **다른 모양**을 남기면 집계가 갈라진다 — 같은 함수·같은 반환이어야."""
    src = inspect.getsource(bot.run_once)
    # 조기 return 은 kill switch·데이터 없음뿐이어야 한다(sells_only 전용 return 금지).
    early = [ln.strip() for ln in src.split("\n")
             if ln.strip().startswith("return {") and '"ok": False' in ln]
    assert all("sells_only" not in ln for ln in early)


# ─────────────────────────── 루프 소유권 ───────────────────────────

def test_loop_ownership_is_exclusive(monkeypatch, tmp_path):
    """두 번째 프로세스는 루프를 잡지 못한다 — 잡으면 같은 종목을 두 번 산다."""
    monkeypatch.chdir(tmp_path)
    store: dict = {}
    monkeypatch.setattr(api.db, "kv_get", lambda k: store.get(k))
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: store.__setitem__(k, v))

    import os as _os

    assert api._claim_loop_ownership() is True          # 첫 프로세스
    monkeypatch.setattr(_os, "getpid", lambda: 999999)  # 다른 프로세스인 척
    assert api._claim_loop_ownership() is False, "두 번째 워커가 루프를 잡았다"


def test_expired_lease_is_taken_over(monkeypatch, tmp_path):
    """죽은 프로세스가 소유권을 영구히 들고 있으면 **아무도 매매하지 않는다** — 만료되면 가져온다."""
    monkeypatch.chdir(tmp_path)
    store = {"loop_owner": {"owner": "dead@host", "at": 0}}   # 아주 오래된 임대
    monkeypatch.setattr(api.db, "kv_get", lambda k: store.get(k))
    monkeypatch.setattr(api.db, "kv_set", lambda k, v: store.__setitem__(k, v))
    assert api._claim_loop_ownership() is True


def test_unreadable_lock_blocks_instead_of_fail_open(monkeypatch, tmp_path):
    """**게이트를 못 읽으면 막는다.** fail-open 은 게이트가 없는 것과 같다(레포 규칙)."""
    monkeypatch.chdir(tmp_path)

    def boom(_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(api.db, "kv_get", boom)
    assert api._claim_loop_ownership() is False


def test_lease_is_renewed_by_the_fast_tick():
    """갱신을 느린 틱(30분)에만 두면 임대에 가까워져 살아 있는데도 소유권을 뺏긴다."""
    src = inspect.getsource(api._quote_loop)
    assert "_renew_loop_ownership" in src
    assert api._LOOP_LEASE_SEC > 30 * 60, "임대가 느린 틱보다 짧으면 두 벌이 돈다"


# ─────────────────────────── 첫 화면 호출 수 ───────────────────────────

def test_inactive_detail_tabs_are_lazy():
    """**비활성 탭은 탭을 눌렀을 때 받는다.** 서버 동시성이 1이라 호출 수가 곧 대기 시간이다.

    실측(2026-08-08): 첫 화면 21개 호출이 11.2초에 정착했고, 병렬 4,430ms가 순차 3,008ms보다
    **느렸다**(= 직렬화). 그중 `동종`·`일정`은 기본 탭(`지표·목표가`)이 아닌데도 종목을 고를
    때마다 즉시 나갔고, `일정`은 **외부 DART를 쳐서 콜드에 16.3초**였다.
    """
    from pathlib import Path

    html = Path("src/signal_desk/web/index.html").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in html.split("\n") if not ln.strip().startswith("//"))
    # 종목 선택 경로에서 즉시 부르면 안 된다.
    assert "renderPeers(ticker);" not in body, "동종을 종목 선택 시 즉시 받는다"
    assert "renderEvents(ticker);" not in body, "일정을 종목 선택 시 즉시 받는다"
    assert "markLazyDetail(" in body and "_LAZY_DETAIL" in body
    # 같은 종목으로 두 번 받지 않는다(탭을 오갈 때마다 외부 DART를 치면 안 된다).
    assert "_lazyLoaded[key] !== _selectedTicker" in body, "탭 전환마다 다시 받는다"
