---
version: 1
slug: "src-signal-desk-web-investment-js"
primary_target: "src/signal_desk/web/investment.js"
related_targets: ["src/signal_desk/web/investment.css"]
---

# 내 보유와 계좌 · PD3

Mode: Operate. 실제 앱 셸·Pretendard·기존 indigo/중립 토큰 계승. 새 브랜드나 루트 DESIGN.md를 임의로 덮어쓰지 않는다.

읽는 순서는 현재 시장 → 실계좌 조회 → 연결/주문 확인 → 분석용 입력 → 새 진단 → 접힌 과거 기록과 보조 도구. 분석용 입력을 실제 계좌라고 부르지 않는다. 복사는 국내·해외 전체를 바꾸는 명시적 동작이며 조회만으로 수행하지 않는다. 실계좌 실패≠빈 보유, 과거 진단≠현재 판단, 화면 조회 시각≠시세 기준시각이다.

불명확한 현금·한도·입력으로 진단을 실행하지 않는다. 실패와 재시도는 선택 설정 안에 숨기지 않는다. 시장/동일시장 응답 경합에서 예전 데이터와 통화를 재사용하지 않는다. 실제 주문 승인과 기존 배당·계좌 딥링크, 차트는 보존한다.

구현 경계와 source/capture provenance: `docs/PRODUCT-DESIGN-PD3-2026-10-18.md`, `.impeccable/review/pd3/captures.json`. 합성 캡처는 실제 계좌나 수익성 증거가 아니다. 독립 리뷰와 정확한 CI·배포 검수는 구별한다. 소유자 응답 대기는 2026-10-18 자동 후속 승인으로 기술 인수의 필수 관문에서 제외하되 수용을 날조하지 않는다.
