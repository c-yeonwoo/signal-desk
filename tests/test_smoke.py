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
    # 개인 페이퍼 봇 조작 UI는 전부 사라져야 한다(트레이딩은 읽기 전용)
    for gone in ("executeReservations", "makeReservations", "toggleBot()", "resetBot()",
                 "setSeed()", "runBotNow()", "previewBot()", 'id="bot-seed"', 'id="bot-toggle-btn"'):
        assert gone not in html, f"{gone}가 아직 남아 있다"
    assert "리셋·시드 변경 없음" in html
    assert 'id="bot-us"' not in html
    assert "openHelp()" in html
    assert html.count('onclick="openHelp()"') == 1  # footer only — 시그널 헤더에 고아 버튼 금지
    # sticky footer 셸 회귀 방지 — main이 shrink되면 관리자 긴 페이지에서 footer가 떠버림
    assert "flex:1 0 auto" in html
    assert "margin-top:auto" in html  # footer
    # 종목 상세: 개요는 종목명과 같은 왼쪽 블록(pane-title-block), 최근 행보는 지표 패널
    assert 'pane-title-block' in html and 'id="signal-about"' in html and "sig-about.is-on" in html
    assert "종목 개요" in html and "최근 행보" in html
    # 연습장 카피는 제거 — 개인 모의계좌가 없으므로 그런 장부도 없다
    assert "모의투자 연습장" not in html and "가상 돈으로 연습해보기" not in html
    assert "gotoPaperFromSignal" not in html and "페이퍼에서 같은 규칙으로 추적" not in html
    assert 'id="paper-from-signal"' not in html
    # 히어로 CTA 제거 — 관심종목 등록은 리스트 ★ 하나로 통일(같은 일을 두 곳에서 하지 않는다)
    assert "trackFromSignal" not in html and "관심종목에 추가하고 변동 알림 받기" not in html
    assert 'class="fav-star' in html and "toggleFav(" in html
    assert 'data-cseg="hypo"' in html and 'id="cycle-seg-hypo"' in html
    assert 'id="hypo-graph"' in html and "drawHypothesisTree" in html
    assert "orient: 'LR'" in html and "roam: true" in html
    assert "흐름 생성" in html and "/api/hypothesis" in html
    assert "최근 이슈 흐름" in html
    assert "clim-pill" in html and "climatePill" in html and "기후" in html
    # 시그널 열: td에 flex 금지(행 붕괴) — 안쪽 .sig-pills만 flex
    assert "sig-pills" in html
    assert ".sig-list .sig-cell { display:flex" not in html
    assert "td에 display:flex 금지" in html
    # 진입 품질은 뱃지 옆이 아니라 시그널 pill 호버 툴팁에만
    assert "entryTip" in html and "pricedInTip" in html and "demoteTip" in html
    assert "sigPillTitle" in html
    assert "지금 이 가정과 안 맞음" not in html
    assert "무게↑" not in html and "갈래 무게" not in html
    assert "지금 더 가까움" in html
    assert "다시 볼 종목" in html
    assert "market-bar-end" in html  # 시황 바 오른쪽 슬롯(live·거시토글)
    assert "시장 ZONE" in html and "경기 사이클(확정)" in html  # 이중 국면 라벨 분리
    assert "sellPrecisionRow" in html and "매도 정밀도" in html  # 숏 검토 전제 관측치
    assert "숏 관측용 · 봇 미반영" in html
    # 정밀도 색·해석은 절대값이 아니라 기준선 대비 리프트 — 하락장 오독 방지
    assert "liftColor" in html and "liftNote" in html and "기준선" in html
    assert "prec >= 55" not in html and "sellPrec >= 55" not in html
    # 시그널 판별력 보드(구 증명 OS). D7은 부차 리텐션(계측만 유지).
    assert 'id="proof-os-body"' in html and "loadProofOs" in html and "시그널 판별력" in html
    assert "증명 OS" not in html and "북극성 A" not in html
    assert "/api/proof" in html and "_pitHeroLine" in html
    assert "loadPickReason" not in html  # 관리자 폼 제거 — 시그널 상세 히어로로 흡수
    assert "runHarnessFromProof" in html and "/api/harness/run" in html
    assert 'id="d7-body"' in html and "loadD7" in html and "리텐션 D7" in html
    assert "북극성 D7" not in html
    # 매수 후보가 순위인지 절대문턱인지 화면이 말해야 한다(봇·브리핑과 같은 기준)
    assert "renderTodayCard" in html and ("자금 한도" in html or "익스포저" in html)
    # 문턱이 점수 분포 밖으로 나가면 즉시 보이게 — 2026-07-26에 실제로 벌어졌던 실패
    assert "threshold_above_max" in html and "매수가 산술적으로 불가능합니다" in html
    # 시세가 멈추면 문턱·분위를 어떻게 바꿔도 매일 같은 결과가 나온다
    assert "signal_drift" in html
    # 감사 가설 카드 — 판정이 아니라 관측이라는 문구가 화면에 남아 있어야 한다
    assert "감사 가설" in html
    assert "tests/test_redteam.py" in html
    assert "/api/audit/run" in html and "점수 동결 의심" in html
    assert 'aria-label="종합점수 이상"' in html and 'aria-label="팩터 강도 이상"' in html
    assert "toggleSignalFilterDrawer" in html and 'id="sig-filter-fab"' in html

    # FAB는 footer 위에 띄움 — footer margin-bottom으로 바닥에서 띄우지 않음
    assert "body.chat-fab-on #chat-fab" in html
    assert "body.chat-fab-on footer" not in html
    assert "/detail?market=" in html  # 클릭 시 상세 병렬 fetch
    assert "_ensureSignalChart" in html  # 차트 DOM 파괴 후 재생성(국내 차트 미표시 방지)
    assert "--c-ma20" in html and "--c-price" in html  # 차트 팔레트 = CSS 변수
    assert "--brand-500:#0F6B62" in html or "--brand-500: #0F6B62" in html  # Ink Desk teal
    assert "#4f46e5" not in html  # 구 인디고 잔재 금지
    # 참조되는데 정의가 없으면 색이 조용히 안 먹는다 — 별칭은 :root에 있어야 한다
    root = html.split(":root {", 1)[1].split("}", 1)[0]
    for alias in ("--sell:", "--warn:", "--down:", "--fg:", "--panel-2:", "--mono:"):
        assert alias in root, f"{alias} 미정의"
    assert 'data-cseg="ref"' in html  # 인사이트 참고 서랍
    assert ">페이퍼<" in html  # 탭명 (구 '내 자산')
    # 상태(precision·편중·데스크)는 「오늘」카드 하나. 매수대기·조사후보 퀵칩은 제거.
    assert 'id="sig-today"' in html and 'class="sig-head"' in html
    assert "renderTodayCard" in html
    assert 'id="buylist-card"' not in html
    assert 'id="qf-extwatch"' not in html and 'id="screen-extwatch"' in html
    assert "정밀도 우선" in html
    assert "트레이딩" in html  # 구 '공개 장부' → #282 '모의운용' 후보를 거쳐 확정
    assert "공개 장부" not in html and "공개장부" not in html and "모의운용" not in html
    assert ">장부<" not in html  # UI 라벨 잔재 — 전부 트레이딩
    # 관리자: 오늘 할 일 랜딩 + 짧은 탭명(점검/엔진/성적/뉴스/발행)
    assert 'id="admin-todo"' in html and "오늘 할 일" in html
    assert "enterAdmin" in html and "renderAdminTodo" in html
    assert 'data-aseg="ops"' in html and "점검" in html and "enterAdmin(" in html
    assert "trust-paper-muted" in html  # 페이퍼 승률 ≠ 실측 헤드라인
    assert "고장 아님" in html
    assert "적중률 공개" not in html  # 공개 적중률 카피 폐기
    assert "Strong Buy" in html and "kindHint" in html  # 순위 의미를 드러내는 라벨·힌트
    assert "hero-runup" in html and "발동 후" in html  # Buy+ 히어로: 발동가 대비 수익률
    assert "hero-card.BUY" in html and "rgba(14,122,79" in html  # Buy+ 은은한 배경
    assert 'id="ob-step-desk"' in html  # 온보딩 3스텝: 데스크 용어 안내
    assert "obFinish('paper')" in html or 'obFinish("paper")' in html
    assert "trust-legend" in html
    assert "자동매매 실제 체결" not in html  # 페이퍼≠실제 체결
    assert "_abLine" in html and "얕은 A/B" in html
    assert "accuracy_at_approve" in html


def test_public_ledger_is_read_only(tmp_path, monkeypatch):
    """트레이딩은 조회만 된다 — 개인 페이퍼 봇(켜기·시드·초기화·수동실행)은 제거됐다.

    리셋할 수 있는 장부는 track record가 아니다: 성적이 나쁘면 초기화하면 그만이라 남은 장부만
    좋아 보인다(백테스트에서 경계한 생존편향과 같은 병)."""
    client = _fresh_client(tmp_path, monkeypatch)
    client.post("/api/auth/signup", json={"email": "bot@b.com", "pw": "abcdef"})

    r = client.get("/api/ledger/state?style=balanced")
    assert r.status_code == 200
    body = r.json()
    assert body["style"] == "balanced" and body["market"] == "kr"

    for path, method in [("/api/bot/toggle", "post"), ("/api/bot/run", "post"), ("/api/bot/reset", "post"),
                         ("/api/bot/seed", "post"), ("/api/bot/style", "post"), ("/api/bot/preview", "post"),
                         ("/api/bot/reserve", "post"), ("/api/bot/state", "get")]:
        resp = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 404, f"{path}가 아직 살아 있다 — 개인 장부 조작 경로"


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
    assert d.get("flow_loaded") is False  # 기본 클릭 경로에선 수급 HTTP 생략
    r2 = client.get("/api/signals/005930/chart?flow=1")
    d2 = r2.json()
    assert d2["flow_loaded"] is True
    assert len(d2["flow_foreign"]) == len(history)
    assert d2["flow_foreign"][0] == 0.0
