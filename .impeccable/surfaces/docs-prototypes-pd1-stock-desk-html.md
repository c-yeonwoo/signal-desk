---
version: 1
slug: "docs-prototypes-pd1-stock-desk-html"
primary_target: "docs/prototypes/pd1-stock-desk.html"
related_targets: []
---

# SIG-25 · 기존 종목 화면의 한정 개선 시안

근거: docs/PRODUCT-DESIGN-PD1-2026-10-18.md. 운영 화면과 기존 피드백 대조를 완료했다. 실제 계좌/원문/주문/API/LLM과 연결하지 않는다. 모든 가격·차트·판정은 예시다. 운영 구현은 SIG-26에서 한다.

**THESIS:** 가격·후보 평가·보유에 대한 질문을 분리하고, 반복 설명을 줄여 비교 가능한 목록을 만든다.

**OWN-WORLD:** 현행 밝은 중성색·indigo·의미색·16px 본문과 두 영역 구조를 계승한다. 시안 폰트는 시스템 우선으로 운영 Pretendard 우선과 다르며 완전 토큰 정합을 주장하지 않는다. 새 브랜드를 만들지 않는다.

**STORY:** 목록 → 가격/판정 시각 → 기록상 가격 변화 → 흐름 → 근거/미확인 → 보유 진단의 필요를 구별한다.

**FIRST VIEWPORT:** PC 약340px 목록/상세, 모바일 단일 목록/상세 왕복. 주문 버튼 없음. 지연·결측·로딩·오류는 값 옆에서 명확히 읽힌다.

**FORM:** 기존 화면의 bounded refinement이므로 seed/새 시각 세계 선정은 불필요. 코드 네이티브 HTML/CSS/차트이며 생성 이미지가 아니다.

**FINISH:** unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

마감: 독립 리뷰 ship, 독립 문서 검토 완료, 실제 브라우저15상태 확인. DESIGN.md의 선재 teal/구메뉴 불일치는 PD5에서 다루며 이 시안을 이유로 전역 디자인 문서를 재생성하지 않는다. 캡처 출처는 .impeccable/review/pd1/captures.json이다. 전체 접근성/성능 인증·운영 배포 완료가 아니다.
