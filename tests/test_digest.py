"""아침 브리핑 — 매수 0일에도 보낼 내용이 있는지 · 실측 가드 · 게이트 제외."""

import datetime
import importlib

from fastapi.testclient import TestClient

from signal_desk import config, digest
from signal_desk.signals.engine import SignalResult


def _sig(ticker, name, kind, score, **over):
    base = dict(ticker=ticker, name=name, score=score, kind=kind, confidence=0.5,
                technical_score=0.0, fundamental_score=0.0, has_fundamental=False, reasons=[])
    base.update(over)
    return SignalResult(**base)


_D = datetime.date(2026, 7, 24)  # 금요일


def test_buy_list_and_zone_header():
    out = digest.build_morning(
        signals=[_sig("A", "가", "BUY", 1.5), _sig("B", "나", "STRONG_BUY", 2.4),
                 _sig("C", "먼종목", "HOLD", 0.2)],
        regime_label="중립", threshold=1.2, base_threshold=1.2, date=_D)
    assert "7/24(금) 아침 브리핑" in out
    assert "지금 시장 중립" in out and "매수문턱 1.20" in out
    assert "매수 시그널 2" in out
    assert out.index("나") < out.index("가")      # 점수 내림차순
    assert "먼종목" not in out                    # HOLD·근접 아님 → 미노출
    assert digest.DISCLAIMER in out


def test_zero_buy_day_still_has_body():
    """매수 0일이 정상 동작임을 말하고, 근접 종목으로 대체 내용을 채운다."""
    out = digest.build_morning(
        signals=[_sig("A", "가", "HOLD", 2.6), _sig("B", "먼종목", "HOLD", 0.1)],
        regime_label="조정", threshold=2.8, base_threshold=1.2,
        bump_reasons=["약세 국면 — 매수 기준 +1.0", "외국인·기관 20일 순매도 -3.1조 — 매수 기준 +0.6"],
        date=_D)
    assert "매수 시그널 0" in out and "기다리는 날" in out
    assert "기본 1.20 + 상향 1.60" in out
    assert "약세 국면" in out                      # 왜 문턱이 올랐는지
    assert "매수 근접" in out and "가 +2.60 (0.20 남음)" in out
    assert "먼종목" not in out                    # 갭 2.7 → 근접 아님


def test_event_risk_excluded_from_both_lists():
    out = digest.build_morning(
        signals=[_sig("A", "악재보유", "BUY", 2.0, event_risk=True),
                 _sig("B", "악재근접", "HOLD", 1.1, event_risk=True)],
        regime_label="중립", threshold=1.2, base_threshold=1.2, date=_D)
    assert "매수 시그널 0" in out
    assert "매수 근접" not in out


def test_accuracy_guard_hides_early_precision():
    early = digest.build_morning(
        signals=[], regime_label="중립", threshold=1.2, base_threshold=1.2, date=_D,
        accuracy={"ready": True, "buy_precision_pct": 71.4, "buy_sample": 7,
                  "ic_min_samples": 20, "coverage": {"matured_primary": 7}})
    assert "누적중 · 성숙 7/20" in early and "71.4" not in early

    mature = digest.build_morning(
        signals=[], regime_label="중립", threshold=1.2, base_threshold=1.2, date=_D,
        accuracy={"ready": True, "buy_precision_pct": 57.0, "buy_sample": 34,
                  "primary_horizon": 20, "ic_min_samples": 20,
                  "coverage": {"matured_primary": 34}})
    assert "매수 정밀도 57.0%" in mature and "표본 34" in mature

    none = digest.build_morning(signals=[], regime_label=None, threshold=1.2,
                                base_threshold=1.2, date=_D)
    assert "track record 쌓는 중" in none and "판정 없음" in none


def test_preview_and_test_send_are_admin_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    client = TestClient(api_module.app)

    assert client.get("/api/morning-digest").status_code == 401
    assert client.post("/api/morning-digest/test").status_code == 401

    client.post("/api/auth/signup", json={"email": "u@e.com", "pw": "abcdef12"})
    assert client.get("/api/morning-digest").status_code == 403
    assert client.post("/api/morning-digest/test").status_code == 403


def test_test_send_needs_telegram(tmp_path, monkeypatch):
    """텔레그램 미설정이면 발송을 시도하지 않고 이유를 돌려준다(conftest가 키를 빈 값으로 고정)."""
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    client = TestClient(api_module.app)
    client.post("/api/auth/signup", json={"email": "devcheck@example.com", "pw": "abcdef12"})
    out = client.post("/api/morning-digest/test").json()
    assert out["ok"] is False and "텔레그램" in out["reason"]
    # 미리보기는 스케줄 상태를 알려주되 발송하지 않는다
    pv = client.get("/api/morning-digest").json()
    assert pv["telegram"] is False and pv["hour_kst"] == 7 and pv["sent_date"] is None


def test_app_link_and_prev_day_change():
    """브리핑에 복귀 링크가 없으면 읽고 끝나서 D7에 기여하지 못한다 · 어제 대비 증감."""
    sigs = [_sig("A", "가", "BUY", 1.5), _sig("B", "나", "STRONG_BUY", 2.4)]
    out = digest.build_morning(signals=sigs, regime_label="중립", threshold=1.2,
                               base_threshold=1.2, date=_D,
                               app_url="https://x.example.com/", prev_buy_count=5)
    assert "앱에서 보기 → https://x.example.com//#signal" not in out   # 끝 슬래시 중복 금지
    assert "앱에서 보기 → https://x.example.com/#signal" in out
    assert "매수 시그널 2 (어제 5 → -3)" in out
    assert out.rstrip().endswith(digest.DISCLAIMER.strip())

    same = digest.build_morning(signals=sigs, regime_label="중립", threshold=1.2,
                                base_threshold=1.2, date=_D, prev_buy_count=2)
    assert "매수 시그널 2\n" in same and "어제" not in same      # 변화 없으면 침묵
    assert "앱에서 보기" not in same                            # URL 미설정이면 링크 없음


def test_rank_mode_header_shows_slots_and_exposure():
    """분위 모드 브리핑은 '매수문턱'이 아니라 '매수권 N종목 · 익스포저 M%'를 말한다 —
    화면·봇·브리핑이 서로 다른 기준을 말하면 사용자가 무엇을 믿어야 할지 알 수 없다."""
    sigs = [_sig("A", "가", "BUY", 1.5), _sig("B", "나", "STRONG_BUY", 1.8),
            _sig("C", "다종목", "HOLD", 1.3)]
    out = digest.build_morning(
        signals=sigs, regime_label="조정", threshold=2.4, base_threshold=1.2, date=_D,
        selection={"mode": "rank", "universe": 200, "rank_slots": 6, "cutoff_score": 1.5},
        exposure=0.15, exposure_reasons=["조정 국면 — 기준 익스포저 20%", "거시 비우호 — ×0.8"])
    assert "매수권 상위 6종목/200" in out and "컷오프 +1.50" in out
    assert "익스포저 15%" in out and "조정 국면" in out
    assert "매수문턱" not in out
    assert "매수 시그널 2" in out
    # 근접은 컷오프 기준 — 절대 문턱(2.4)은 더 이상 매수를 정하지 않는다
    assert "매수 근접(컷오프까지" in out and "다종목" in out


def test_rank_mode_zero_buys_is_normal():
    """분위 모드 매수 0일도 정밀도 우선의 정상 — 최소점수·게이트로 자리가 비는 날."""
    out = digest.build_morning(
        signals=[_sig("A", "가", "HOLD", 0.2)], regime_label="약세", threshold=1.2,
        base_threshold=1.2, date=_D,
        selection={"mode": "rank", "universe": 200, "rank_slots": 6, "cutoff_score": None},
        exposure=0.4)
    assert "매수 시그널 0" in out and "기다리는 날" in out
    assert "관리자 확인이 필요" not in out


def test_absolute_mode_header_unchanged():
    out = digest.build_morning(
        signals=[_sig("A", "가", "BUY", 1.5)], regime_label="중립", threshold=1.2,
        base_threshold=1.2, date=_D, selection={"mode": "absolute"})
    assert "매수문턱 1.20" in out and "매수권 상위" not in out


def test_public_base_url_config(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert config.public_base_url() is None
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://a.b/ ")
    assert config.public_base_url() == "https://a.b"


def test_digest_hour_config(monkeypatch):
    monkeypatch.delenv("MORNING_DIGEST_HOUR", raising=False)
    assert config.morning_digest_hour() == 7
    monkeypatch.setenv("MORNING_DIGEST_HOUR", "8")
    assert config.morning_digest_hour() == 8
    monkeypatch.setenv("MORNING_DIGEST_HOUR", "off")
    assert config.morning_digest_hour() is None
    monkeypatch.setenv("MORNING_DIGEST_HOUR", "99")   # 범위 밖 → 발송 안 함
    assert config.morning_digest_hour() is None
