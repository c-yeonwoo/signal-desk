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

**BACKLOG #1~#4 — 시그널 엔진**
- [x] 기술적분석(`signals/indicators.py`) — RSI14/MACD(12,26,9)/MA20,60,120, brightdesk 공식 그대로 이식
- [x] 기본적분석(`signals/fundamental.py`) — DART 연동(실키로 검증 완료), 키 없으면 자동 생략(그레이스풀 폴백)
- [x] 통합 시그널(`signals/engine.py`) — 가용 컴포넌트만 재정규화해 결합, `/api/signals` 실데이터
- [x] 백테스트 성적표 1차(기술점수 단독) — `/api/backtest`
- [ ] 시장 국면 + 매크로 미니차트, 후보 유형+기회도, 저평가 뷰, phase2 전체 — [BACKLOG.md](BACKLOG.md) 참고

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

- **KRX_API_KEY 서비스 이용신청 승인 대기** — "유가증권 종목기본정보"·"유가증권 일별매매정보"를
  openapi.krx.co.kr에서 신청해야 함(키 발급과 별개, 직접 확인). 승인되면 `ingest/krx_open_api.py`의
  시가총액 필드명을 실응답으로 1회 검증 필요(BACKLOG.md §0 참고). 그때까지는 대형주 30종목 폴백.
- 재무 데이터 중 PER/PBR — DART만으로 불가(시가·발행주식수 필요), KRX 종가 연동 후 계산 예정.
- 시장 국면 판정 룰(강세/과열/조정/약세) — 지수·금리·거래대금 임계값 설계 필요.
- 자동매매봇·전망 시나리오의 유사투자자문업 규제 해당 여부 — 법률 자문 필요(BACKLOG.md 하단 참고).
