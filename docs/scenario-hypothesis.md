# 시황 가설 (Scenario Hypothesis) — 설계 메모

> 상태: **수동 Haiku 전체 트리** (`signals/hypothesis.py` · `/api/hypothesis` · ECharts tree)  

> 대응 백로그: [BACKLOG.md](../BACKLOG.md) `#6`  
> 기존 `#9` `signals/scenario.py`(포트폴리오 MC)와 **별개**.

## 1. 한 줄 정의

최근 KB·뉴스에서 뽑은 **거시 핫이슈**를 배타적 IF로 두고,  
그 아래 **지표 then 분기 → outcome** 트리를 보여 주는 **맥락 전용** 기능.

**시그널·봇 미반영.** Layer0+then+outcome 문장은 **관리자 수동 생성 시 Haiku 1회**.  
**지지도 %·status는 룰.** 일일 자동 LLM 호출 없음.

## 2. 원칙

| 항목 | 합의 |
| --- | --- |
| 생성 | 관리자 `POST /api/hypothesis/refresh` 만. 일일 KB 훅에서 호출하지 않음 |
| 모델 | Haiku (`DIGEST_MODEL`). Opus 불필요 |
| Layer0 % | 배타 IF 형제 지지도(룰). 예측 확률 아님 |
| then/outcome | `%` 없음. 라이브 지표 `aligned`/`watching`/`diverging` |
| 엔진 | BUY/SELL·봇·문턱·가중치 미변경 |
| UI | ECharts tree · active IF만 체인 펼침 |

## 3. 파이프라인 (수동 refresh)

1. KB 코퍼스(`_MARKET`·시황/insight·최근 헤드라인) + digest + 직전 가설 라벨  
2. 키워드 TF topN → 프롬프트에 헤드라인·키워드만 (전문 덤프 금지)  
3. Haiku JSON: IF 2~4 + then≤2 + outcome (섹터·metric 화이트리스트)  
4. 룰: 지지도·status·evidence retrieve  
5. 캐시 `hypo:v3:latest` (`source: llm|fallback`, `model`, `generated_at`)

폴백: 키 없음/실패 시 큐레이션 `_TEMPLATES`로 룰만 저장 (`source: fallback`).

## 4. API

| 메서드 | 경로 | 역할 |
| --- | --- | --- |
| GET | `/api/hypothesis` | 캐시만. 없으면 `ready:false` — **자동 생성·LLM 금지** |
| POST | `/api/hypothesis/refresh` | 관리자만. Haiku(+폴백) 생성 |

## 5. UI

- 미생성: placeholder + 관리자 생성 버튼  
- 메타: `Haiku · 수동 · 시각` 또는 `fallback`  
- 기존 트리·IF 칩·상세 패널

## 6. 비목표

- 일일 자동 LLM  
- Opus 가설 생성  
- 시그널/봇 연동  
- then 확률 %  
