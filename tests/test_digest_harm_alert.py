"""손해 경보가 아침 브리핑에 실린다.

2026-09-06 진단: `bot.harm_alert` 는 `/api/weekly-track`(관리자)에서만 계산됐다.
3봇 모두 "시장보다 못한 것이 통계적으로 확정" 상태(상한 −4.12 · −6.33 · −6.51%p)로
2주가 지났는데 어느 알림에도 뜨지 않았다 —
"shadow 관측은 '판정 알림'까지 만들어야 끝난다".
"""

from __future__ import annotations

import datetime

from signal_desk import digest


def _harm(label, upper, alert=True, blocks=8):
    return {"label": label, "ready": True, "alert": alert, "upper_pp": upper,
            "excess_pp": upper - 1, "blocks": blocks, "days": 43}


def test_alert_renders_one_line_with_the_bound():
    line = digest.harm_line([_harm("안정형", -4.12), _harm("균형형", -6.33)])
    assert line is not None
    assert "손해 경보" in line
    assert "-4.1%p" in line and "-6.3%p" in line
    assert "안정형" in line and "균형형" in line
    assert "블록 8개" in line, "표본 크기 없이 경고만 내면 읽는 사람이 세기를 모른다"
    assert line.count("\n") == 0, "한 줄이어야 한다 — 길면 아래 내용을 밀어낸다"


def test_silent_when_nothing_is_confirmed():
    """정상일 때는 아무 말도 하지 않는다 — 매일 초록불은 곧 안 읽힌다(정지 배너와 같은 규약)."""
    assert digest.harm_line([]) is None
    assert digest.harm_line(None) is None
    assert digest.harm_line([_harm("균형형", +1.2, alert=False)]) is None


def test_not_ready_is_not_an_alert():
    """표본이 모자란 상태를 경고로 번역하지 않는다(0의 이유 규칙)."""
    assert digest.harm_line([{"label": "균형형", "ready": False, "alert": False,
                              "upper_pp": None, "blocks": 0}]) is None


def test_morning_puts_it_above_the_market_header():
    body = digest.build_morning(
        signals=[], regime_label="중립", threshold=1.2, base_threshold=1.2,
        date=datetime.date(2026, 9, 7),
        selection={"mode": "rank", "universe": 200, "rank_slots": 6},
        harm=[_harm("균형형", -6.33)])
    assert "손해 경보" in body
    assert body.index("손해 경보") < body.index("지금 시장"), \
        "아래로 밀면 안 읽힌다 — 정지 배너 바로 다음이어야 한다"


def test_morning_without_harm_is_unchanged():
    kw = dict(signals=[], regime_label="중립", threshold=1.2, base_threshold=1.2,
              date=datetime.date(2026, 9, 7),
              selection={"mode": "rank", "universe": 200, "rank_slots": 6})
    assert digest.build_morning(**kw) == digest.build_morning(**kw, harm=[])


def test_api_sends_it_and_shares_one_implementation():
    """브리핑과 weekly-track이 **같은 함수**를 쓴다 — 두 곳에서 조립하면 갈라진다."""
    src = open("src/signal_desk/api.py", encoding="utf-8").read()
    assert "harm=_harm_alerts(" in src, "브리핑에 손해 경보가 안 넘어간다"
    assert src.count("bot.harm_alert(") == 1, \
        "harm_alert 호출이 두 곳이면 화면과 알림이 갈라진다"
