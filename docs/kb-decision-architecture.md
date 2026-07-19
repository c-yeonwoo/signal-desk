# KB → Decision 아키텍처

> 상태: **합의·착수** (2026-07-19)  
> 관련: [BACKLOG.md](../BACKLOG.md) `#11` · `#3` 정성 팩터 · 조사 후보(`external_watch`)

## 1. 목표 · 비목표

**목표**
- KB 수집 풀(유튜브·블로그·RSS·공시·뉴스)을 점진 확대하되 **검증바는 높게** 유지.
- Sonnet 다이제스트 비용을 **수익률·리스크 회피**에 회수 (전시용 요약만이 아님).
- **KB + 시그널 엔진**이 서비스 핵심. 잘못된 KB가 점수에 **환각·오염**을 일으키면 안 됨.

**비목표**
- LLM 자유 문장을 `combine()` 가중합에 바로 넣기.
- 거시(`_MARKET`) 톤을 개별 종목 점수에 이중 계상.
- 자동매매를 LLM 에이전트로 만들기 (실행은 결정론 코드).

## 2. 레이어 계약

```text
Collect (화이트리스트 · high validate)
   → Trust tier (official / high / medium / low · shadow)
   → Structured event card (유형·방향·심각도·근거·TTL)
   → Decision          Attention
      veto / 문턱 / 우선순위     조사 후보 · 괴리 UI · buylist
      (실측 후) 정성 팩터 승격
```

| 레이어 | 입력 | 출력 | 엔진 점수 |
|---|---|---|---|
| Collect | 원문 | entry + validation | 영향 없음 |
| Trust | source + verdict | `decision_eligible` | 영향 없음 |
| Event | confirmed + 근거 | event card | 영향 없음 |
| Decision | active events + 시그널 | veto·action·threshold | **점수가산 아님** |
| Attention | 괴리·후보 | watch / UI | 영향 없음 |
| Qualitative (P3) | 실측 통과 feature | 제한적 priority/threshold | `combine` 직접 투입 금지(기본) |

## 3. 원칙

1. **엔진에 들어가는 것은 문장/감성이 아니라 구조화 필드.**
2. **비대칭:** 악재(veto)는 강하게, 호재는 약하게(주의·우선순위). 환각 호재로 BUY를 만들지 않음.
3. **confirmed + decision_eligible** 만 Decision 입력. `review`/`rejected`/`shadow`는 RAG·가설·관리 UI만.
4. **근거(evidence) 없는 이벤트는 confirmed 불가.**
5. **실측 track record 통과 전**에는 `weight_qualitative`를 `combine()`에 넣지 않음 (현재와 동일).
6. Sonnet 호출은 **보유·조사후보·BUY근접·이벤트 의심** 종목 우선 (유니버스 전량 금지).

## 4. Source trust

| tier | 예 | Decision | 비고 |
|---|---|---|---|
| `official` | DART 공시 | 규칙만으로 eligible 가능 | P0 첫 대상 |
| `high` | 큐레이션 전문가·기관 RSS | Opus validate 후 | trusted 완화바 가능 |
| `medium` | 화이트리스트 뉴스·채널 | Opus validate 필수 | |
| `low` / shadow | 실험 소스 | 저장·검색만 | 엔진 금지 |

validate bar(종목 import Opus · 거시 Opus)는 **유지·강화**. 풀이 늘수록 티어 분리가 더 중요.

## 5. Event card (필드)

- `event_key` — 중복 병합 키 (예: `dart:{rcept_no}`)
- `scope_type` — `stock|sector|market`
- `ticker` / `sector`
- `event_type` — `delisting|accounting|capital_raise|contract|litigation|...`
- `direction` — `negative|positive|mixed|unknown`
- `severity` — `info|watch|serious|critical`
- `status` — `candidate|confirmed|rejected|expired|resolved`
- `decision_eligible` · `decision_action` — `none|attention|threshold_bump|buy_block|trim|exit`
- `confidence` · `trust_tier`
- `detected_at` · `effective_at` · `expires_at` · `resolved_at`
- `summary` · `rationale` · `policy_version` · `extractor_model`
- evidence: `entry_id`/`url`/`evidence_text`/`support_role`

## 6. Decision 정책 (요약)

현재 동작을 카드 기반으로 재현·이관:
- `critical` → 신규 매수 차단 · 보유 전량 청산
- `serious` → 신규 매수 차단 · 보유 부분 축소
- 동일 이벤트를 **점수 감점 + veto**에 이중 반영하지 않음

이후 확장(P2):
- 문턱 ± (작은 값, 국면·실측 가드)
- 후보 풀 안 우선순위 재정렬
- Attention rate-limit

정성 팩터 승격(P3): `off|shadow|priority|threshold` — 기본 off/shadow, 관리자 승인 + 표본·워크포워드 게이트.

## 7. Attention

- 정성호재 + 점수 근접 → 조사 후보
- 강한 BUY vs serious 이벤트 → 괴리
- 악재 해제/만료 → 재평가 큐
- 가설 outcome 섹터 → 국내 대표 종목 조사 후보

`external_watch`와 통합하되 **수동/자동 출처를 구분**.

## 8. 롤아웃

| Phase | 목표 | 상태 |
|---|---|---|
| **P0** | DART → event card → 기존 veto/봇 인터페이스 유지 · 읽기 API | **착수** |
| **P1** | source registry · 전 수집기 공통 ingest gate · Sonnet candidate 이벤트 · 관리 UI | 예정 |
| **P2** | `decision.py` 단일 정책 · Attention · 시그널 상세 이벤트 | 예정 |
| **P3** | 실측 게이트 후 qualitative 승격(shadow→priority/threshold) | 예정 |

### P0 수용 기준
- 주요 DART 공시가 `kb_events` (+ evidence)로 저장
- `sentiment_map()`이 active 이벤트에서 `event_risk`/`event_severity` 산출 (레거시 digest 플래그 폴백)
- 미확정·근거 없는 카드는 decision 입력 불가
- 기존 봇 critical/serious 동작 회귀 유지

## 9. 감사 · 롤백

- 결정 로그에 `event_id` · `policy_version` 기록 (P2)
- 이벤트 최초 수집 시각 ≠ 가격 반응 시각 (look-ahead 금지, P3)
- 성과 악화 시 qualitative 모드 즉시 off/shadow

## 10. 구현 메모 (코드 위치)

| 관심사 | 위치 |
|---|---|
| 스키마 · CRUD | `db.py` (`kb_events`, `kb_event_evidence`) |
| 공시→카드 · sentiment | `kb.py` (+ 추후 `kb_events` 헬퍼 분리 가능) |
| 정책 단일화 | `signals/decision.py` (P2) |
| 조사 큐 | `external_watch.py` |
| 정성 표시 | `signals/qualitative.py` (점수 미가산 유지→P3) |
