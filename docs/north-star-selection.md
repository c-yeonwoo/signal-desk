# 북극성 — 선택 품질 (A)

> 합의(2026-08-03): 이 앱의 1순위는 **잘 고르는가(+ 그 이유)** 다.  
> 외부 소비자 리텐션(D7)이 아니다. 페이퍼 봇은 시그널·타이밍 증명용 종속 지표.

관련: [verification-system.md](verification-system.md) · [desk-hierarchy.md](desk-hierarchy.md) ·
[kb-decision-architecture.md](kb-decision-architecture.md) · [north-star-d7.md](north-star-d7.md)

## 한 줄

**북극성 A = 시그널이 종목을 고르는 판별력(IC · shadow · harness)과, 고른 이유를 사후 재생할 수 있는가.**

## 층 (충돌 시 타이브레이커)

| 층 | 이름 | 질문 | 승격/머지 |
|---|---|---|---|
| **A** | 선택 품질 | 눈이 좋아졌나? | **필수** — IC lift · shadow `verdict_ready` · harness 판별력 |
| **B** | 페이퍼 타이밍 | 그 시그널이 체결 경로에서도 사나? | 참고 — PnL이 아니라 체결·스킵·게이트 정합 |
| **C** | Decision 회피 | 악재 veto가 사고를 줄였나? | Decision/KB PR만 1순위 |

B가 나빠져도 A가 유의한 개선이면 엔진 변경을 롤백하지 않는다(사이징·국면 노출 이슈일 수 있음).  
C를 세게 해서 B가 줄어도, C 전용 성적이 받쳐 주면 Decision 변경을 A/B만으로 죽이지 않는다.

## 종합(거시·산업·KB) 계약

입으로는 “종합 시그널”이지만 **L0 `combine()`에는 8팩터만** 들어간다.

| 입력 | 위치 | 점수 가산 |
|---|---|---|
| 기술·기본·저평가·낙폭·수급·퀄리티·모멘텀·숏 | L0 combine | ✅ |
| KB 정성 (`weight_qualitative`) | shadow / 향후 priority | ❌ (실측 승격 전) |
| KB 이벤트 | Decision veto | ❌ (차단·청산만) |
| 국면·거시 | `target_exposure` · 문턱 bump | ❌ (크기/자격 게이트) |
| hypo / climate / 산업 사이클 UI | 학습·Attention | ❌ |

거시·산업·이슈를 알파로 쓰려면 **A 층 shadow를 이긴 뒤** Decision·exposure·priority로만 승격한다.
`combine` 직접 투입은 Later · 별도 게이트.

## 증명 OS

`GET /api/proof` — A를 1열, B/C를 참고열로 모은다.  
픽 이유: `GET /api/pick-reason?date=&ticker=` (PIT ⊕ 실현수익 ⊕ 봇 저번).

## 비북극성 (동결·Later)

- D7 · 온보딩 확장 · 숏폼 성장 · 외부 사용자 리텐션  
- 소비자 Empty UX 실험(강의 부대가 다시 열리기 전)

정의·계측이 남아 있어도 **작업 우선순위에서 A에 양보**한다.
