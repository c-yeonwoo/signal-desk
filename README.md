# signal-desk

**Signal Desk** — "감이 아니라 검증된 적중률로" 주식 매매 타이밍을 찾는 논스톱 시그널 플랫폼.

[Signal APT](https://github.com/c-yeonwoo/apt-signal)(데이터로 찾는 아파트 매매 타이밍)의
서비스 본체·아키텍처를 이식하고, 자산을 부동산에서 주식으로 바꿔 재해석한 프로젝트.
현재 [`brightdesk`](../brightdesk)에서 시도했던 주식 시그널 기능 중 일부를 이 뼈대 위에
체리피킹해 얹는 중이다 — 배경은 [CLAUDE.md](CLAUDE.md), 체리피킹 대상은
[NOTES-cherrypick.md](NOTES-cherrypick.md) 참고.

## 현재 상태 (MVP — 엔진·봇·UI 가동)

- ✅ FastAPI + 인증 + 온보딩 미니플로우(성향·관심종목 3개)
- ✅ 8팩터 시그널 엔진 + 4게이트(국면·추세·어닝·KB veto) + 실측 트래커(`/api/accuracy`)
- ✅ 페이퍼 자동매매봇(유저별 계좌) + 레퍼런스 봇 track record
- ✅ SPA 4탭: **시그널 · 내 자산 · 인사이트 · 관리자**(+마이페이지)
  - 시그널: 리스트/차트·스크리너·매수대기·신뢰 스트립(실측/누적중/시뮬 가드)
  - 내 자산: 시그널 트레이딩 · 내 포트폴리오 · 배당
  - 인사이트: 사이클 · 밸류체인 · 학습 · 거장 · ETF(맥락 레이어)
- ✅ 신뢰 UI: 미성숙 track record는 숫자 비공개, 백테스트는 `시뮬` 배지
- ⏳ 실측 트래커 성숙(~20거래일) 후 팩터 가중 재확정·공개 성과 대시보드
- ⬜ 유료화 화이트리스트 게이트(기록만, H3)

다음 우선순위·의존관계는 [BACKLOG.md](BACKLOG.md) · 핸드오프 참고.
## 설치 & 실행

> ⚠️ Python 3.12 권장 (Signal APT 이력상 3.14는 pandas/pyarrow 세그폴트 — 이 리포는 아직
> pandas 미도입이라 당장은 무관하나 2단계부터 유효).

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/sigdesk serve      # http://127.0.0.1:8765

.venv/bin/pytest -q
```

## 구조

```
src/signal_desk/
├── api.py           # FastAPI — 인증·시그널·봇·KB·관리자
├── auth.py          # pbkdf2 세션
├── bot.py           # 페이퍼 자동매매(멀티테넌트)
├── brain.py         # 두뇌 레이어(읽기 전용 헬스)
├── signalcfg.py     # 팩터 가중·임계값 영속화
├── broker/paper.py  # 유저별 페이퍼 계좌
├── signals/         # 엔진·팩터·게이트·accuracy·narrative
├── ingest/          # KRX·DART·FRED·네이버·RSS 등
├── reference/       # 사이클·밸류체인·거장·quant_methods
└── web/index.html   # 단일 파일 SPA
```

## 환경변수

`.env.example` 참고. 실제 키는 `.env`에 넣고 절대 커밋하지 않는다.

주요 운영 변수:

| 변수 | 용도 |
|---|---|
| `APP_ENV=prod` | prod 모드 — 세션 쿠키 `secure` 플래그(HTTPS 전제) |
| `BROKER_BACKEND=paper` | 자동매매는 **페이퍼 전용**(실주문 경로 제거). KIS 모듈은 레거시/참고 |
| `KIS_ENV=demo` | KIS 모의투자(권장). 실계좌는 `demo` 외 값 + `ALLOW_REAL_ORDERS=true` 필요 |
| `ALLOW_REAL_ORDERS` | 실계좌 실주문 이중 안전장치(기본 off) |
| `BOT_KILL_SWITCH` | 긴급정지 — 켜면 어떤 주문도 안 나감 |
| `BOT_DAILY_LOSS_LIMIT_PCT=0.08` | 당일 손실 한도 초과 시 신규매수 중단 |
| `ADMIN_EMAILS` | 관리자(엔진·KB 적재·데이터 갱신) 화이트리스트 |
| `FANDING_TT`·`OUTSTANDING_AUTHORS`·`YOUTUBE_CHANNELS` | KB 외부 소스(세션토큰·작가·채널) |
| `ANTHROPIC_API_KEY` | LLM 다이제스트·해설·자문(없으면 규칙기반 폴백) |

## 배포 — Railway

**데이터 저장소는 파일 기반**이다(별도 DB 서버 불필요):

| 저장 | 형식 | 위치 |
|---|---|---|
| 계정·세션·워치리스트·보유·봇 포지션·거래·KB·알림 | **SQLite** | `data/cache/app.db` |
| 시세·재무·유니버스·거시·거장 스냅샷 | parquet·json | `data/cache/*.{parquet,json}` |

Postgres/MySQL/Redis 안 씀 — SQLite 단일 파일이라 **단일 인스턴스(numReplicas=1)** 로만 운영한다.
가족 규모엔 충분하고, 나중에 다중 인스턴스가 필요해지면 그때 Postgres로 이관.

### Railway 세팅

1. **레포 연결** → Railway가 `Dockerfile`을 자동 감지(빌드 설정은 `railway.json`).
2. **Volume 필수** — Railway 컨테이너 파일시스템은 재배포 시 초기화되므로, 볼륨을 **`/app/data`** 에
   마운트해야 계정·시세·KB가 유지된다. (안 붙이면 재배포마다 전부 날아감)
3. **환경변수**(Variables 탭) — `.env.example` 참고. 최소 권장:
   - `APP_ENV=prod` (secure 쿠키), `ADMIN_EMAILS=<내 이메일>`
   - `KRX_API_KEY`·`DART_API_KEY`·`FRED_API_KEY` (실데이터), `ANTHROPIC_API_KEY` (해설·자문)
   - `KIS_*`(모의투자, `KIS_ENV=demo`) 또는 `BROKER_BACKEND=paper`
   - KB 소스: `FANDING_TT`·`FANDING_DEVICE_UID`, `YOUTUBE_API_KEY`, (선택) `OUTSTANDING_*`
   - `ALPHAVANTAGE_API_KEY` (US 시총·PER)
   - `PORT`은 Railway가 자동 주입 → 그대로 사용(코드가 `$PORT` 바인딩).
4. 배포되면 `https://<앱>.up.railway.app` 로 접속.

### 배포 후 실데이터 적재 (버튼)

로그인(첫 가입 계정을 `ADMIN_EMAILS`에 넣으면 관리자) → **관리자 탭**에서:

1. **데이터 갱신**(`/api/refresh`) 1회 — KOSPI 유니버스·시세·재무·거시(FRED)·거장 13F·S&P500 유니버스
   ·거장 보유 US 시세를 한 번에 적재. **이걸 눌러야 시그널/백테스트/저평가가 실데이터로 채워진다.**
2. **미주은/아웃스탠딩/유튜브 수집** — KB 외부 소스(수동 1회, 이후 서버가 하루 1회 자동 증분수집).
3. US 발행주식수·PER는 서버가 하루 20개씩 자동 백필(AV 25콜/일 한도 → S&P500 전량 ~25일).

⚠️ **최초 데이터 갱신 후 절대값 검증**: 샌드박스 캐시엔 종목별 시세 스케일 이슈가 있었으므로, prod에서
실 KRX 피드로 적재한 뒤 삼성전자 등 종가·시총이 실제와 맞는지 한 번 확인할 것.
※ 전체 S&P500 US 시세(거장 보유 외) 백필 버튼은 아직 없음 — 필요 시 별도 추가.

## 배포 — 기타(도커/자체 서버)

서버(uvicorn)가 자동매매 루프·KB 일일수집을 in-process로 함께 돌린다 —
**컨테이너/프로세스를 항상 켜두면 별도 스케줄러가 필요 없다**(단, 프로세스가 죽으면 멈추므로
docker restart 정책이나 systemd로 상시 기동 보장).

```bash
# 1) 도커
docker build -t signal-desk .
docker run -d --name signal-desk --restart unless-stopped \
  -p 8765:8765 --env-file .env -v signal_desk_data:/app/data signal-desk

# 2) 최초 1회 데이터 적재(컨테이너 안에서) — 실 시세/재무 캐시 생성
docker exec signal-desk sigdesk fetch
```

- **HTTPS**: 앞단에 리버스 프록시(Caddy/Nginx)로 TLS 종단 + `APP_ENV=prod`로 secure 쿠키.
- **DB 백업**: SQLite/parquet 캐시는 `/app/data` 볼륨 → 주기적 스냅샷 백업 권장.
- **데이터 신뢰성**: 샌드박스 캐시 시세는 종목별 스케일 이슈가 있으므로, prod 최초 `sigdesk fetch`로
  실 KRX/증권사 피드를 적재하고 절대값(백테스트·시나리오)이 합리적인지 확인할 것.
- **KB 수집**: 서버가 하루 1회 미주은·오건영·유튜브를 자동수집(증분). `FANDING_TT` 세션토큰은
  만료 시 `.env` 갱신 필요.
