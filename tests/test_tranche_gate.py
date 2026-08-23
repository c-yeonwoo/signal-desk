"""분할매수가 3분 간격으로 몰리고 상한을 넘겼다(2026-08-22 프로덕션 실측).

## 측정 (최근 체결 120건 · 진입 에피소드 25개)

    ADD가 붙은 에피소드   23/25 (92%)
    ADD 횟수 (중위/최대)   2회 / 6회
    설정 상한 초과        15/23 (65%)
    추가 간격 중위        2.9분   · 최소 18초
    10분 이내 추가        55/61건 (90%)

## 두 결함

① **간격 게이트가 없었다.** `entry_tranches` 주석은 "진입 타이밍 리스크 분산"이라고
   약속했는데, 3분 간격으로 4번 사는 건 분산이 아니다 — 한 번에 사는 것과 사실상 같고
   수수료만 배로 낸다. 그리고 평단이 오르면 손절·트레일링선도 같이 올라가 더 쉽게 걸린다
   (`DB하이텍`: 11분에 6번 매수 → 5시간 뒤 트레일링 청산).

② **회차를 세지 않았다.** 목표비중 도달(95%)로만 막았는데, 정수 주수 반올림 때문에 한 회차가
   의도한 금액보다 훨씬 적게 채워지고(고가주는 1주) 목표에 못 닿아 다음 루프에서 또 산다.

## 규약

- 간격은 **날짜**로 센다. 시각으로 "24시간"을 세면 금요일 마감 뒤 토·일에 시계만 흘러
  월요일에 두 번째 회차가 바로 열린다.
- 회차 기록이 없으면 **막지 않는다** — 모르는 것을 막으면 그게 곧 0으로 나누기다.
- `INSERT OR REPLACE` 라서 스냅샷 갱신이 회차를 지우지 않는지도 검사한다(지우면 상한이
  아무 것도 안 막는다).
"""

from __future__ import annotations

import inspect
import re

from signal_desk import bot, strategy


# ---------- 게이트 판정 ----------

def test_second_tranche_waits_for_the_next_day():
    """**이게 그 버그다.** 같은 날 두 번째 회차가 열리면 3분 간격 매수가 된다."""
    pos = {"tranches_done": 1, "last_buy_date": "2026-08-22"}
    ok, why = bot.tranche_gate(pos, 3, today="2026-08-22")
    assert not ok and "하루 1번" in why
    ok2, _ = bot.tranche_gate(pos, 3, today="2026-08-23")
    assert ok2, "다음 날에는 열려야 한다 — 안 그러면 분할이 아니라 단일 진입이다"


def test_cap_is_counted_explicitly():
    """목표비중만 보면 정수 반올림 때문에 상한을 넘는다 — 회차를 직접 센다."""
    for done, tr, blocked in ((2, 3, False), (3, 3, True), (4, 3, True), (1, 2, False), (2, 2, True)):
        ok, why = bot.tranche_gate({"tranches_done": done, "last_buy_date": "2026-08-01"},
                                   tr, today="2026-08-22")
        assert ok is (not blocked), (done, tr, why)
        if blocked:
            assert f"{done}/{tr}" in why, "몇 회 중 몇 회인지 안 적으면 조사가 안 된다"


def test_unknown_position_is_not_blocked():
    """모르는 것을 막으면 0으로 나누기다 — 수동 편입 포지션을 영구 차단하면 안 된다."""
    assert bot.tranche_gate(None, 3, today="2026-08-22") == (True, None)
    assert bot.tranche_gate({}, 3, today="2026-08-22") == (True, None)


def test_zero_cap_disables_the_cap_but_keeps_the_interval():
    """상한 0은 '끄기'다. 그래도 간격은 남아야 한다 — 끄는 값이 둘을 같이 끄면 안 된다."""
    ok, _ = bot.tranche_gate({"tranches_done": 99, "last_buy_date": "2026-08-01"}, 0,
                             today="2026-08-22")
    assert ok
    ok2, why = bot.tranche_gate({"tranches_done": 99, "last_buy_date": "2026-08-22"}, 0,
                                today="2026-08-22")
    assert not ok2 and "하루 1번" in why


def test_missing_last_buy_date_does_not_block():
    """마이그레이션 직후 기존 행은 날짜가 없다 — 그걸로 막으면 전 포지션이 멈춘다."""
    ok, _ = bot.tranche_gate({"tranches_done": 1, "last_buy_date": None}, 3, today="2026-08-22")
    assert ok


# ---------- 배선 ----------

def test_the_add_loop_actually_calls_the_gate():
    """게이트를 만들고 안 부르면 그건 없는 것과 같다."""
    src = inspect.getsource(bot.run_once)
    i = src.index('"reason": "ADD"')
    assert "tranche_gate(" in src[:i], "ADD 직전에 게이트를 안 부른다"


def test_add_records_the_tranche_and_date():
    """기록하지 않으면 다음 루프가 같은 회차를 무한히 다시 센다."""
    src = inspect.getsource(bot.run_once)
    i = src.index('"reason": "ADD"')
    blk = src[i:i + 1600]
    assert "tranches_done=" in blk and "last_buy_date=" in blk


def test_new_entries_start_at_tranche_one():
    """**팔고 다시 산 종목**이 옛 회차를 이어받으면 상한이 즉시 걸린다(로테이션 재편입 경로)."""
    src = re.sub(r"^\s*#.*$", "", inspect.getsource(bot), flags=re.M)
    # `"SIGNAL"` 은 매도 사유 문자열에도 쓰여 단순 검색이 엉뚱한 곳을 잡는다 — **매수 로그
    # 호출**을 앵커로 쓴다(체결을 남기는 자리 바로 뒤가 포지션 기록이다).
    logs = [m.start() for m in re.finditer(r'"buy", qty, \w+, "(SIGNAL|ROTATE_IN|RESERVATION)"', src)]
    assert len(logs) >= 2, f"신규 매수 로그 자리를 {len(logs)}곳만 찾았다 — 패턴이 바뀌었다"
    for i in logs:
        blk = src[i:i + 700]
        assert "tranches_done=1" in blk, (
            f"신규 진입이 회차를 1로 시작하지 않는다:\n    {src[i:i+240]}")


def test_skips_are_reported_not_silent():
    """"왜 추가가 안 됐나"가 어느 화면에도 안 뜨면 그게 조용한 0이다."""
    src = inspect.getsource(bot.run_once)
    assert "skipped_tranche" in src
    assert '"skipped_tranche": skipped_tranche' in src, "결과에 안 실으면 닿을 수 없다"


def test_entry_tranches_presets_are_positive():
    """상한이 0이면 이 게이트의 절반이 꺼진다 — 설정이 약속을 지키는지 본다."""
    for style in ("conservative", "balanced", "aggressive"):
        assert strategy.entry_tranches(style) >= 2, style
