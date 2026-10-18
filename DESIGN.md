---
name: Signal Desk
description: 시장·종목·내 투자를 근거와 시각으로 구별하는 투자 작업대
colors:
  primary: "#243CDB"
  background: "#F6F7F9"
  panel: "#FDFDFC"
  text: "#151A22"
  secondary-text: "#526071"
  border: "#CBD2DC"
  buy: "#0B7655"
  sell: "#CB2F2A"
typography:
  body:
    fontFamily: "Pretendard, -apple-system, BlinkMacSystemFont, Apple SD Gothic Neo, sans-serif"
    fontSize: "16px"
    lineHeight: 1.6
rounded:
  sm: "6px"
  md: "8px"
  lg: "16px"
spacing:
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "#fff"
    rounded: "{rounded.sm}"
    padding: "8px 14px"
---

# Signal Desk — 디자인 시스템

## Overview

현재 구현과 소유자가 승인한 PD5 범위에 맞춘 기준이다. 과거 teal·시그널/페이퍼/인사이트 탭·단일 HTML 설명은 현재 코드와 충돌해 정정했다. 새 리브랜딩이나 색상 변경이 아니다. 구체적인 작업 흐름은 `docs/PRODUCT-DESIGN-PROJECT-2026-10-18.md`와 해당 PD 문서를 따른다. UI 검수 완료는 예측 정확도나 수익 개선의 증거가 아니다.

## Colors

실행 원본은 `src/signal_desk/web/design-tokens.css`다. 위 값은 재사용 중인 토큰의 문서화이며 실제 변경은 CSS와 함께 갱신한다. 브랜드 `--accent`는 선택 상태와 주요 행동, `--sig-buy`/`--sig-sell`은 매매 방향에 쓴다. 가격선은 `--c-price`, 시그널 점수선은 `--c-score`다. 색·서체를 임의로 추가하지 않는다.

## Typography

Pretendard 한 계열과 시스템 폴백을 쓴다. 본문은 위 body 규격, 재사용 척도는 `--fs-2xs`~`--fs-2xl`이다. 일부 기존 조밀한 표·메타데이터에는 작은 일회성 크기가 남아 있다. 모든 화면이 통일됐다고 주장하지 않는다. 새 설명은 기존14~16px 척도를 우선하고 제목과 설명을 붙여 놓지 않는다. 숫자 비교에는 tabular numerals를 쓴다.

## Layout

앱 셸은 **오늘 · 종목 · 내 투자 · 관리자**, 종목 하위는 **국내 · 해외 · 관심종목**이다. 기존 딥링크·권한을 유지한다. 옛 메뉴를 문서만 보고 되살리지 않는다.

`index.html`은 셸·종목·시장·관리자, `home.js/css`는 투자 요약, `investment.js/css`는 보유/진단 흐름이다. 공통 토큰은 별도 CSS에 있다. 문서형 화면은 최대1360px, 시장 카드는 최대1400px, 종목 작업대는 가용 폭을 쓴다. 모바일은900/640px 등 실제 컴포넌트 경계에서 접는다. 목록/상세 왕복·표 내부 스크롤을 유지하고 페이지 전체 가로 넘침으로 해결하지 않는다. 제목→설명→본문은 한 묶음, 다음 섹션은 더 크게 띄운다. 공통 spacing 척도를 우선한다.

## Elevation & Depth

`--shadow-sm`은 기존 셸/약한 경계, `--shadow`는 기존 떠 있는 면에 쓴다. 반복 카드·새 그림자·그라데이션을 장식 목적으로 추가하지 않는다. 현재 예외를 신규 규칙으로 확대하지 않는다.

## Shapes

작은 조작은 `--r-sm`, 연결 조작은 `--r-md`, 큰 패널은 `--r-lg`를 따른다. pill은 작은 상태·선택에만 쓴다. 툴팁·메뉴가 overflow로 잘리지 않는지 확인한다.

## Components

- `.btn`/`.btn.primary`, `.ctrl`, `.seg-toggle`를 재사용한다. hover·disabled·실패·로딩을 구분하며 실제 비활성화에는 native `disabled`가 필요하다. `aria-disabled` 스타일만으로 동작을 막았다고 보지 않는다.
- 데이터 없음·수집 중·조회 실패·가격 지연·연구 표본 대기를 같은 문장으로 합치지 않는다. 오류를0/빈값/최신값으로 바꾸지 않고 재시도와 범위를 설명한다.
- 표는 숫자와 단위를 함께 표시한다. 모바일에서 핵심 상태·기준일·손실을 지우지 않는다. 긴 이름은 줄바꿈한다.
- 관리자는 문제→영향→확인 시각→다음 행동을 먼저 보여준다. 자동 대기·연구 참고는 따로 접는다. 이미 분리된 메뉴를 다시 군집화하지 않는다.
- 계좌 조회와 실주문 권한은 다르다. 봇 성향→추종 비율→계좌 한도, 저장값/편집초안, 사전점검·명시적 승인, 미저장 변경·늦은 응답 방어를 보존한다. UI 검수로 실제 주문이나 계좌 변경을 하지 않는다.

## Do's and Don'ts

- 가격·시그널 점수·B/S·매수/매도 구간·툴팁·간단/추세/자세히 모드를 보존한다.
- 실시간 표시 가격과 종가 기반 판정 시각, 관찰 표본과 시장 전체, 지난 진단과 새 판단을 구별한다.
- 쉬운 문장을 먼저 쓰고 기술명·수치·원문은 설명에서 찾을 수 있게 한다. 원문 미확인을 호재·악재 없음으로 쓰지 않는다.
- 장식 시황 그림·매일 상위 LLM 편집·검증되지 않은 수익 주장을 추가하지 않는다.
- 시그널/순위·API·주문 의미를 프론트엔드 정리의 부수 효과로 바꾸지 않는다.
