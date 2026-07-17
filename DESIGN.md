# Signal Desk — 디자인 시스템

> 신규 기능·화면 추가 시 **이 문서의 토큰·규칙을 먼저 따른다.** 새 색·폰트·라운드를 임의로 만들지 말 것.
> 모든 스타일은 `src/signal_desk/web/index.html` 단일 파일(인라인 + `<style>`)에 있음.

## 1. 브랜드

- **아이덴티티**: "감이 아니라 규칙·게이트·성적" — Ink Desk(책상)·정밀도·타이밍. 강의 실습용 무료 데스크.
- **브랜드 컬러 = Teal `#0F766E`** (`--accent`). CTA·선택 상태만. 매수/매도 색과 분리.
- 워드마크 `Signal Desk`. 부제 "규칙으로 쌓는 매매 타이밍".
- 공개 적중률 마케팅·유료 SaaS 과금 전제 없음. 구 인디고 `#4f46e5` 사용 금지.

## 2. 토큰 (`:root`, 단일 소스)

```
표면   --bg #F3F1EC · --panel #fff · --line #e5e2da · --txt #0B1220 · --dim #5c6578
브랜드 --accent #0F766E · --accent-ink #fff · --accent-weak #ecfdf8 · --ring rgba(15,118,110,.28)
시그널 --sig-buy #0B8F5A · --sig-sell #C23B3B · --sig-watch #c2410c · --sig-neutral #8b93a7
차트   --c-price #1a2233(ink) · --c-score #0B8F5A · --c-ma20/60/120 · --c-rsi #64748b
형태   --r-sm 6 · --r-md 10 · --r-lg 12 · --shadow 절제
```

- **색은 반드시 토큰 사용.** hex 하드코딩 금지(특히 브랜드색 = `var(--accent)`).
- **시그널 색은 매매 방향에만.** 브랜드 teal과 섞지 말 것.
- **차트 가격선은 ink**, 점수선이 시각적 주인공.

## 3. 타이포

- 본문: Pretendard (CDN 로드) → 시스템 폴백.
- 스케일: `--fs-xs` 11 … `--fs-xl` 20.

## 4. 컴포넌트·IA 규칙

- 상단 탭: **시그널 · 페이퍼 · 인사이트** (+ 관리자·마이페이지).
- 인사이트: 사이클 · 밸류체인 · **참고**(학습/거장/ETF 2단).
- 시황 적응 문구: 기본 칩, 탭하면 사유 펼침(mweb 세로 예산 보호).
- 신뢰 스트립(`#signal-trust`): 시그널 상단 해자 — 숨기지 말 것(데이터 없으면 누적중).
- mweb(≤900): 리스트 nested scroll 금지, `overflow-x: clip`, FAB는 safe-area.

## 5. 금지

- AI형 인디고/보라 그라데이션 히어로
- 카드·pill·그림자 과다로 “대시보드 슬롭” 만들기
- 매수 초록을 브랜드 accent로 전용하기
