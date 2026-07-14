# Signal Desk — 디자인 시스템

> 신규 기능·화면 추가 시 **이 문서의 토큰·규칙을 먼저 따른다.** 새 색·폰트·라운드를 임의로 만들지 말 것.
> 모든 스타일은 `src/signal_desk/web/index.html` 단일 파일(인라인 + `<style>`)에 있음.
> Signal APT([`DESIGN.md`](https://github.com/c-yeonwoo/apt-signal/blob/main/DESIGN.md))의
> 구조·규칙을 그대로 따르되 브랜드 컬러만 구분한다.

## 1. 브랜드

- **아이덴티티**: "감이 아니라 검증된 적중률" — 신뢰·데이터·차분함(Signal APT와 동일 톤).
- **브랜드 컬러 = 인디고 `#4f46e5`** (`--accent`). Signal APT의 로열블루(`#2563eb`)와 구분되는
  Signal Desk 고유 색. 워드마크 `Signal Desk`. 부제 "검증된 적중률로 찾는 주식 매매 타이밍".

## 2. 토큰 (`:root`, 단일 소스)

```
표면   --bg #f8fafc · --panel #fff · --line #e2e8f0 · --txt #1e293b · --dim #64748b
브랜드 --accent #4f46e5 · --accent-ink #fff · --accent-weak rgba(79,70,229,.10) · --ring rgba(79,70,229,.22)
시그널 --sig-strong #22c55e · --sig-buy #16a34a · --sig-watch #d97706 · --sig-neutral #94a3b8 · --sig-sell #dc2626
형태   --r-sm 8 · --r-md 12 · --r-lg 16 · --shadow 0 8px 24px rgba(2,32,71,.09)
```

- **색은 반드시 토큰 사용.** hex 하드코딩 금지(특히 브랜드색 = `var(--accent)`).
- **시그널 색은 자산과 무관하게 Signal APT와 동일 의미 체계 유지**: STRONG_BUY=`#22c55e`,
  BUY=`#16a34a`, WATCH=`#d97706`, NEUTRAL=`#94a3b8`, SELL_RISK=`#dc2626`.

## 3. 타이포 스케일 (px)

| 용도 | 크기 |
|---|---|
| 보조/캡션 | 11 (`--dim`) |
| 기본 소형(칩·표) | 12 |
| 본문 | 13 |
| 카드 제목·라벨 | 14 |
| 섹션/버튼 강조 | 15~16 |
| 페이지 h2 | 19~22 |
| 히어로 수치 | 24~34 |

## 4. 컴포넌트 규칙

- **버튼**: Primary는 `background:var(--accent); color:#fff` — accent 배경엔 항상 흰 글씨.
  Ghost/보조는 `.btn`(패널 배경 + `--line` 테두리). 위험은 `color:var(--sig-sell)`.
- **칩/pill**: `.chip`(11px), 활성 `.chip.on`(accent 배경 + 흰 글씨).
- **카드**: `1px solid var(--line)` + `border-radius:var(--r-lg)` + hover `--shadow`.
- **모달(dialog)**: `border-radius:16px(--r-lg)`, 헤더 = 제목(좌) + ✕(우).
- **폴백**: 데이터·키 없으면 조용히 숨기거나 안내(토스트/점선 박스) — 에러 화면 금지.

## 5. 라운드·간격

- 라운드: 작은 요소 8(`--r-sm`), 카드/입력 12(`--r-md`), 모달/큰 카드 16(`--r-lg`).
- 간격: 4·8·12·16·20 기준.
- 포커스: 입력 focus 시 `box-shadow:0 0 0 3px var(--ring)`.

## 6. 내비게이션

- 상단 탭(현재): **시그널 · 내 자산 · 인사이트** (+ 관리자·마이페이지).
  - 시그널: 매수/매도 타이밍 · 스크리너(저평가 흡수) · 매수 대기 · 신뢰 스트립
  - 내 자산: 페이퍼 봇 · 실보유/리밸런싱 · 배당
  - 인사이트: 사이클 · 밸류체인 · 학습 · 거장 · ETF — **맥락 레이어**(엔진 신호 아님)
- 탭 무한 증식 금지 — 새 기능은 기존 탭 안 세그먼트나 유틸 칩으로 편입.
- **의견≠신호**: 엔진 수치는 `.layer-badge.sig`, KB·챗·거장·ETF는 `.layer-badge.ctx`.
- **신뢰 가드**: track record 미성숙=`.trust-badge.accum`(누적중), 백테스트=`.trust-badge.sim`(시뮬).
- 지도(Leaflet)는 사용하지 않는다 — 섹터/테마는 트리맵·히트맵(ECharts).

## 7. 이모지 · 톤

- 장식 이모지 절제. 의미 기호(★ 즐겨찾기·✕ 닫기·⚠ 경고)만 최소 사용.
- 신뢰·데이터·차분 톤 — 과장된 확신 표현 지양("무조건", "100%" 등 금지).

## 8. 신규 기능 추가 체크리스트

1. 색: `var(--accent)`·시그널 토큰만 사용했나? accent 배경에 흰 글씨인가?
2. 폰트/라운드: 스케일(§3·§5) 안에서 골랐나?
3. 카드/모달/버튼: 기존 클래스 재사용했나?
4. 키·데이터 없을 때 graceful 폴백이 있나?
5. 탭을 새로 늘리기 전에 기존 그룹/유틸 칩으로 되는지 검토했나?
