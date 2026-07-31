# Nightly Product Reviewer (Lx) — 스케치

> 상태: **모듈 스케치** (2026-07-31). 자동 스케줄·타사 provider는 후속.  
> 정본 위계: [desk-hierarchy.md](desk-hierarchy.md) · 감사와 구분: [verification-system.md](verification-system.md)

## 역할

매매 경로 **밖**에서 제품을 검열한다.

| | Audit (`audit.py`) | Product Reviewer (`product_reviewer.py`) |
|---|---|---|
| 질문 | 이 **숫자**가 틀렸을 수 있나? | 이 **제품/데이터/UX**에 구멍이 있나? |
| 예 | 룩어헤드·분모 오염 | 미분류 crowded · 빈날 카피 · 이중감점 |
| 권한 | 없음 (가설만) | 없음 (BACKLOG 후보만) |
| 판정 | 기계(레드팀) | 사람 승인 후 이슈화 |

둘 다 `falsifier` 없으면 버린다. 엔진·봇·문턱을 자동으로 바꾸지 않는다.

## 입력 스냅샷

- `desk_report` (오늘 L4)
- `selection` / `crowding` (data_quality 포함)
- `advisor_shadow` 요약 (paired)
- `data_health` drift
- 최근 UI 카피 규칙 위반 후보(표현 규칙 체크리스트)

## 출력 계약

```json
{"findings": [{
  "area": "data|ux|risk|cost|copy",
  "title": "40자 이내",
  "claim": "무엇이 왜 문제인지",
  "falsifier": "무엇을 보면 이 지적이 거짓인가",
  "severity": "high|medium|low",
  "backlog_hint": "BACKLOG에 쓸 한 줄"
}]}
```

## 프로바이더

현재 런타임 LLM은 Anthropic 단일(`llm.py`).  
스케치 단계에서는 같은 키로 **역할만 분리**한 프롬프트를 쓴다.  
타사 교차검열(OpenAI 등)은 env `PRODUCT_REVIEW_PROVIDER` 로 붙일 자리만 남겨 둔다 —  
같은 공급사 상호동의 착시를 줄이려면 교차가 맞지만, 키 없이 경로를 열지 않는다.

## 운영

- 수동: `POST /api/product-review/run` (관리자)
- 자동: 봇 루프/cron에 야간 1회 훅 (후속) — 매매 틱과 분리
- 저장: `kv product_review_last` (최근 1회). 이슈화는 사람이 BACKLOG로.

## 하지 말 것

- 당일 매수 선별·사이즈에 쓰기
- “매일 수익을 내라”는 개선안
- falsifier 없는 일반론 (“모니터링 강화”)
