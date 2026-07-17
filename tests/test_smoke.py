import importlib

from fastapi.testclient import TestClient


def _fresh_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    return TestClient(api_module.app)


def test_index_served(tmp_path, monkeypatch):
    client = _fresh_client(tmp_path, monkeypatch)
    r = client.get("/")
    assert r.status_code == 200
    assert "Signal Desk" in r.text


def test_api_requires_auth(tmp_path, monkeypatch):
    client = _fresh_client(tmp_path, monkeypatch)
    r = client.get("/api/signals")
    assert r.status_code == 401


def test_signup_login_profile_flow(tmp_path, monkeypatch):
    client = _fresh_client(tmp_path, monkeypatch)
    r = client.post("/api/auth/signup", json={"email": "a@b.com", "pw": "abcdef"})
    assert r.status_code == 200 and r.json()["ok"]

    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["auth"] is True
    assert r.json()["onboarded"] is False

    r = client.put("/api/profile", json={"투자성향": "balanced", "desk_onboarded": True})
    assert r.status_code == 200

    r = client.get("/api/profile")
    assert r.json()["투자성향"] == "balanced"
    assert r.json()["desk_onboarded"] is True
    assert client.get("/api/auth/me").json()["onboarded"] is True

    r = client.post("/api/favorites", json={"kind": "ticker", "key": "005930", "label": ""})
    assert r.status_code == 200
    r = client.get("/api/favorites")
    assert {"kind": "ticker", "key": "005930", "label": ""} in r.json()["favorites"]

    r = client.get("/api/signals")
    assert r.status_code == 200
    assert r.json()["ready"] is False
    assert r.json()["items"] == []


def test_index_has_trust_and_onboard_ui(tmp_path, monkeypatch):
    client = _fresh_client(tmp_path, monkeypatch)
    html = client.get("/").text
    assert 'id="signal-trust"' in html
    assert "trust-badge" in html
    assert 'id="onboardDlg"' in html
    assert "desk_onboarded" in html
    assert "누적중" in html
    assert ">시뮬<" in html or "시뮬</span>" in html
    assert "layer-badge" in html
    assert "8팩터" in html
    assert 'id="bot-acct-status"' in html
    assert 'id="w_qualitative"' not in html
    assert "executeReservations" in html
    assert 'id="bot-exec-res-btn"' in html
    assert "페이퍼 계좌" in html
    assert 'id="bot-us"' not in html
    assert "ON은 국내·해외 공통" in html
    assert "openHelp()" in html
    assert html.count('onclick="openHelp()"') == 1  # footer only — 시그널 헤더에 고아 버튼 금지
    # sticky footer 셸 회귀 방지 — main이 shrink되면 관리자 긴 페이지에서 footer가 떠버림
    assert "flex:1 0 auto" in html
    assert "margin-top:auto" in html  # footer
    # 종목 상세: 사업 개요(🏢) + 최근 행보(사실 요약) 블록 + 리스트 슬림/상세 분리
    assert "🏢" in html and "최근 행보" in html
    assert "/detail?market=" in html  # 클릭 시 상세 병렬 fetch
    assert "_ensureSignalChart" in html  # 차트 DOM 파괴 후 재생성(국내 차트 미표시 방지)
    assert "--c-ma20" in html and "--c-price" in html  # 차트 팔레트 = CSS 변수
    assert "--brand-500:#0F766E" in html or "--brand-500: #0F766E" in html  # Ink Desk teal
    assert "#4f46e5" not in html  # 구 인디고 잔재 금지
    assert 'data-cseg="ref"' in html  # 인사이트 참고 서랍
    assert ">페이퍼<" in html  # 탭명 (구 '내 자산')
    assert 'id="sig-precision"' in html
    assert "gotoPaperFromSignal" in html
    assert "정밀도 우선" in html
    assert "적중률 공개" not in html  # 공개 적중률 카피 폐기
    assert "매수권" in html  # evidence-only 라벨
    assert 'id="ob-step-desk"' in html  # 온보딩 3스텝: 데스크 용어 안내
    assert "obFinish('paper')" in html or 'obFinish("paper")' in html
    assert "trust-legend" in html
    assert "자동매매 실제 체결" not in html  # 페이퍼≠실제 체결
    assert "_abLine" in html and "얕은 A/B" in html
    assert "accuracy_at_approve" in html


def test_bot_state_and_toggle(tmp_path, monkeypatch):
    client = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "bot@b.com", "pw": "abcdef"})

    r = client.get("/api/bot/state")
    assert r.status_code == 200
    assert r.json()["enabled"] is False  # 기본값 OFF

    r = client.post("/api/bot/toggle", json={"enabled": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/api/bot/state").json()["enabled"] is True


def test_bot_manual_run_reports_reason_when_not_configured(tmp_path, monkeypatch):
    client = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "e@b.com", "pw": "abcdef"})
    r = client.post("/api/bot/run")
    assert r.status_code == 200
    assert r.json()["ok"] is False  # KIS 키 없는 테스트 환경이라 정상적으로 실패 사유 반환


def test_signal_chart_no_data(tmp_path, monkeypatch):
    client = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "c@b.com", "pw": "abcdef"})
    r = client.get("/api/signals/005930/chart")
    assert r.status_code == 200
    assert r.json() == {"ready": False, "dates": []}


def test_signal_chart_with_data(tmp_path, monkeypatch):
    client = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "d@b.com", "pw": "abcdef"})

    from signal_desk import api as api_module
    from signal_desk.ingest import naver
    history = [{"date": f"2026-01-{i:02d}", "close": 100.0 + i} for i in range(1, 26)]
    monkeypatch.setattr(api_module.store, "load_price_history", lambda ticker: history)
    monkeypatch.setattr(naver, "investor_flow_series", lambda code, days=120: [
        {"date": h["date"], "foreign_net": float(i), "inst_net": float(-i), "volume": 1000}
        for i, h in enumerate(history)
    ])

    r = client.get("/api/signals/005930/chart")
    assert r.status_code == 200
    d = r.json()
    assert d["ready"] is True
    assert d["dates"] == [h["date"] for h in history]
    assert d["close"] == [h["close"] for h in history]
    assert len(d["ma20"]) == len(history)
    assert len(d["rsi"]) == len(history)
    assert "macd" in d and "macd_signal" in d and "macd_hist" in d
    assert "scores" in d and len(d["scores"]) == len(history)
    assert len(d["flow_foreign"]) == len(history)
    assert d["flow_foreign"][0] == 0.0
