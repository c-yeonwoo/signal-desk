# CLAUDE.md — signal-desk

"감이 아니라 검증된 적중률로 매수/매도 시그널"을 주는 주식 매매 타이밍 논스톱 플랫폼.
자매 프로젝트 [Signal APT](https://github.com/c-yeonwoo/apt-signal)(아파트 매매 타이밍)의
서비스 본체·아키텍처를 ~75% 그대로 이식하고, 자산이 부동산→주식으로 바뀐 만큼 내용물만
재해석한다. 기존 주식 프로토타입(`brightdesk`)의 기능·인사이트는 20~30%만 체리피킹—
자세한 내용은 [NOTES-cherrypick.md](NOTES-cherrypick.md) 참고.

## 무엇인가

시장국면 → 섹터/테마 → 밸류에이션(저평가) → 후보(눌림목·낙폭과대·IPO·실적서프라이즈·턴어라운드)
→ 개별 종목 → 비교 → AI 리포트로 이어지는 흐름. 핵심 신뢰 해자는 **성적표(백테스트 적중률)** —
과거 시그널이 실제로 맞았는지 정량 검증해 상시 노출한다(2단계 이후 구현).

## 기술 스택

- **Python 3.11~3.13** (3.14 금지 — Signal APT에서 pandas/pyarrow datetime 추론 세그폴트
  exit 139 이력. 이 리포는 아직 pandas 미도입이라 즉시 리스크는 없지만 2단계부터 적용)
- FastAPI + uvicorn, typer(CLI), ECharts(CDN, 프론트 차트)
- 프론트: 단일 파일 `src/signal_desk/web/index.html` (인라인 vanilla JS SPA, 해시 라우팅)
- 인증: 표준 라이브러리 pbkdf2 세션(`auth.py`) — 외부 의존성 0
- 데이터: SQLite(`data/cache/app.db`) — 2단계부터 parquet/json 캐시 추가 예정

## 빌드 & 실행

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/sigdesk serve       # http://127.0.0.1:8765
.venv/bin/pytest -q
```

## 현재 상태

**1단계 — 스캐폴딩**
- [x] FastAPI 앱 골격 + 인증(회원가입/로그인/세션) + 온보딩 프로필 API
- [x] 워치리스트(즐겨찾기) CRUD API
- [x] 단일 파일 SPA 셸: 브랜드 토큰 · 5탭 해시 라우팅 · 인증 게이트 · 온보딩 모달

**BACKLOG #0~#4 — 데이터 소스 + 시그널 엔진**
- [x] 유니버스 — KRX Open API 시가총액 상위 200(보통주만, 코스피200 근사) 실키 검증 완료.
  키 없거나 서비스 미승인이면 대형주 30종목 폴백(`ingest/krx.py`)
- [x] 기술적분석(`signals/indicators.py`) — RSI14/MACD(12,26,9)/MA20,60,120, brightdesk 공식 그대로 이식
- [x] 기본적분석(`signals/fundamental.py`) — DART(ROE·부채비율·매출성장) + DART×KRX 결합(PER·PBR)
  전부 실키 검증 완료. 키 없으면 자동 생략(그레이스풀 폴백)
- [x] 통합 시그널(`signals/engine.py`) — 가용 컴포넌트만 재정규화해 결합, `/api/signals` 실데이터
- [x] 백테스트 성적표 1차(기술점수 단독) — `/api/backtest`, 200종목 표본으로 BUY 승률 50.8%
- [x] 밸류에이션(저평가) 스크리닝(BACKLOG #13) — `signals/valuation.py`, `/api/valuation`, 저평가 탭 실데이터
- [x] 시그널 탭 차트 UI — apt-signal 스타일 좌:우 5:5(종목리스트/차트), dataZoom 기간조절,
  과거 시그널 구간 markArea, 규칙기반 해설(`signals/narrative.py`, v1 — v2는 BACKLOG #17)
- [x] 리스크 엔진(BACKLOG #8) — `signals/risk.py`, stop-loss/take-profit/trailing 순수 함수
  (포지션 모델은 #7 자동매매봇과 함께 올 예정)
- [x] KIS 모의투자 연동 — `broker/kis.py`(인증 토큰 캐시·잔고조회·주문실행) 실키 검증(잔고 1억원 확인)
- [ ] 자동매매봇(#7) 나머지 — 시그널→주문 자동 루프, 포지션 추적, #8 리스크 엔진 연결
- [ ] 시장 국면 + 매크로 미니차트(#5·#6), phase2 나머지 — [BACKLOG.md](BACKLOG.md) 참고

다음에 붙일 기능의 상세 우선순위·범위·의존관계는 [BACKLOG.md](BACKLOG.md) 참고.

## 핵심 개념 (부동산 → 주식 번역, 요약)

| Signal APT | Signal Desk |
|---|---|
| 지역(region) | 섹터/테마 |
| 단지(complex) | 종목(ticker) |
| 급지(A~D) | 시총 등급 / 밸류에이션 분위 |
| 경기순환 국면 | 시장 국면(강세·과열·조정·약세) |
| 저평가(입지 대비 가격) | 저평가(PER·PBR·성장 대비) |
| 매물 유형(경매·급매·청약) | 후보 유형(눌림목·낙폭과대·IPO·실적서프라이즈·턴어라운드) |
| 지도(Leaflet) | 섹터/테마 트리맵·히트맵(지리 없음) |
| 규제지역 오버레이 | 이벤트 오버레이(관리종목·투자경고·공매도과열·공시) |

자세한 아키텍처 대응은 노션 "Signal APT 완전 정복" 빌드 로그(2026-07-02) 참고.

## 데이터 규칙

- `data/raw/*`, `data/cache/*`는 커밋 금지(.gitignore 처리됨). 실제 API 키 커밋 금지 —
  `.env.example`에만 키 이름 문서화.
- 시그널 임계값·가중치는 하드코딩 금지, 설정 모듈 한 곳에 모을 것(2단계 `signals/engine.py`).
- `println`/`print` 디버그 금지 — 로거 사용.

## 미해결 / 확인 필요

- 시가총액 상위 200은 실제 코스피200 편입종목과 리밸런싱 시점 등으로 소폭 다를 수 있음(진짜
  편입종목 리스트는 공식 API에 없음 — BACKLOG.md §0 참고, 정확도가 중요해지면 data.krx.co.kr
  수동 다운로드로 교체 검토).
- 시장 국면 판정 룰(강세/과열/조정/약세) — 지수·금리·거래대금 임계값 설계 필요.
- 자동매매봇·전망 시나리오의 유사투자자문업 규제 해당 여부 — 법률 자문 필요(BACKLOG.md 하단 참고).
- **KIS 토큰 발급(oauth2/tokenP) rate limit 주의** — 짧은 간격으로 재요청하면 HTTP 403.
  `broker/kis.get_token()`을 거치지 않고 직접 발급 API를 호출하지 말 것(파일 캐시 우회 금지).
