"""외부 소스 수집 연도 하한(INGEST_MIN_YEAR) — 2026 이전은 스킵."""

from signal_desk import kb


def test_year_ok_cutoff():
    assert kb.INGEST_MIN_YEAR == 2026
    assert kb._year_ok("2026-07-03 14:00:00") is True
    assert kb._year_ok("2026-01-01T00:00:00+00:00") is True
    assert kb._year_ok("2025-12-31T23:59:59Z") is False
    assert kb._year_ok("2019-05-01") is False
    # 날짜 불명(빈값·비표준)은 포함(정보 유실 방지)
    assert kb._year_ok("") is True
    assert kb._year_ok(None) is True
    assert kb._year_ok("unknown") is True
