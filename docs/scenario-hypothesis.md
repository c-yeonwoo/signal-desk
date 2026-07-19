# 최근 이슈 흐름 — 설계 메모

> 상태: **수동 Sonnet 파급 트리** (`signals/hypothesis.py` · `/api/hypothesis` · ECharts tree)  

> 대응 백로그: [BACKLOG.md](../BACKLOG.md) `#6`  
> 기존 `#9` `signals/scenario.py`(포트폴리오 MC)와 **별개**.

## 1. 한 줄 정의

최근 KB·뉴스에서 뽑은 **관심 이슈** 아래,  
**이 방향 / 다른 방향** 쌍갈림 → 산업·지표 파급(outcome)을 펼치는 **학습·시황 읽기** 기능.

**가설 검증이 아님.** 시그널·봇 미반영.  
Layer0 문장은 **관리자 수동 생성 시 Sonnet 1회**.  
**관심%·갈래 무게%는 룰.** 일일 자동 LLM 호출 없음.

## 2. 원칙

| 항목 | 합의 |
| --- | --- |
| 생성 | 관리자 `POST /api/hypothesis/refresh` 만. 일일 KB 훅에서 호출하지 않음 |
| 모델 | Sonnet (`DIGEST_QUALITY_MODEL` / `NARRATIVE_MODEL`). 경제 인과 품질 우선 |
| 이슈 % | 이슈 간 관심 비중(룰). 예측 확률 아님 |
| 갈래 | 항상 `path`+`alt` 쌍. 갈래 무게% + `emphasized`(지금 지표와 더 겹치는 쪽) |
| 상태 배지 | 없음(aligned/watching/diverging 폐기). 트리·해설·%만 |
| 엔진 | BUY/SELL·봇·문턱·가중치 미변경 |
| UI | ECharts tree · 강조 갈래 굵게 · active 이슈 칩 |

## 3. 파이프라인 (수동 refresh)

1. KB 코퍼스(`_MARKET`·시황/insight·최근 헤드라인) + digest + 직전 이슈 라벨  
2. 키워드 TF topN → 프롬프트에 헤드라인·키워드만 (전문 덤프 금지)  
3. Sonnet JSON: 이슈 1~3 + 각 이슈 path/alt 쌍 + outcome (섹터·metric 화이트리스트)  
4. 룰: 이슈 관심% · 갈래 무게% · evidence retrieve  
5. 캐시 `hypo:v4:latest` (`source: llm`, `model`, `generated_at`)

실패 시 기존 캐시 유지 + `ready:false` (고정 템플릿으로 조용히 덮지 않음).  
폴백 `_TEMPLATES`는 테스트·로컬 `build()`용.

## 4. API

| 메서드 | 경로 | 역할 |
| --- | --- | --- |
| GET | `/api/hypothesis` | 캐시만. 없으면 `ready:false` — **자동 생성·LLM 금지** |
| POST | `/api/hypothesis/refresh` | 관리자만. Sonnet(+검증) 생성 |

## 5. UI

- 탭명: **최근 이슈 흐름** · 버튼: **흐름 생성**
- 미생성: placeholder + 관리자 생성 버튼  
- 메타: `Sonnet · 수동 · 시각`  
- 트리·이슈 칩·상세 패널(해설·참고 지표·섹터·근거 뉴스)

## 6. 비목표

- 일일 자동 LLM  
- Opus 생성  
- 시그널/봇 연동  
- 가설 검증·예측 확률 %  
- aligned/diverging 상태 배지  
