# 시황 가설 (Scenario Hypothesis) — 설계 메모

> 상태: **IF 트리 재설계** (`signals/hypothesis.py` · `/api/hypothesis` · ECharts tree)  

> 대응 백로그: [BACKLOG.md](../BACKLOG.md) `#6` 하반기 전망  
> 기존 `#9` `signals/scenario.py`(포트폴리오 MC)와 **별개** — 이름만 비슷한 다른 기능.

## 1. 한 줄 정의

시황·정책에 대한 **배타적 IF 가설**과, 그 아래 **지표 then 분기 → 결과(outcome)** 를  
트리로 보고, Layer0에만 **지지도(상대 가중 %)** 와 근거 뉴스를 붙인 뒤  
관심 섹터 → 기존 시그널로 딥링크하는 **맥락 전용** 기능.

**시그널 엔진·페이퍼 봇 점수에는 미반영.** 독립 레이어. LLM/Opus로 가지를 생성하지 않음.

## 2. 합의된 원칙


| 항목    | 합의                                              |
| ----- | ----------------------------------------------- |
| Layer0 % | **지지도** — 배타적 IF 형제 상대가중, 합 100%. 시장 예측 확률 아님 |
| then/outcome | `% 없음`. 라이브 지표로 `aligned` / `watching` / `diverging` |
| 갱신    | **하루 1회**(기존 KB 일일 수집 후) + **수동 새로고침**(관리자) |
| 엔진 영향 | 없음. BUY/SELL·봇·매수 문턱·가중치 미변경                    |
| 표현    | 가설·학습용. 투자 권유·수익 보장·적중률 헤드라인 금지                 |
| UI    | ECharts `tree` (TB). active IF만 then 체인 확장     |


면책: `가설·학습용 · 지지도(배타 가설 간 상대 가중) · IF 분기 · 예측·투자권유 아님 · 시그널과 별개`

## 3. 사용자 플로우

1. 인사이트 탭 → 서브 `시황 가설`
2. 루트 아래 3개 **배타적 IF**와 지지도 % (트리)
3. 지지도 최대 IF가 기본 펼침 → then → outcome 선으로 읽음
4. 다른 IF 클릭 시 그 체인만 펼침 (동시에 세 갈래 예측처럼 보이지 않음)
5. 노드 클릭 → 가정·조건·현재값·근거 뉴스·관심 섹터 딥링크

## 4. UI

### 4.1 배치

- 인사이트(`view-cycle`) 서브탭. 새 탭 금지.

### 4.2 다이어그램

- **ECharts `tree`**, orient TB, polyline edge.
- Layer0 IF 노드는 항상 표시 + `지지도 N%`.
- active IF의 자손만 `collapsed: false`, 나머지 IF는 접힘.
- 노드 색: status(`aligned`/`watching`/`diverging`) 또는 kind별.

### 4.3 상세 패널

- 가정 · 조건(현재값) · 관심 섹터 · 근거 URL · 면책

## 5. 데이터·점수

### 5.1 노드 모델

```
ScenarioTree
  id, as_of, disclaimer, source_rev
  nodes (nested children):
    id, parent_id, kind          # if | then | outcome
    label, edge                  # if|then|and|but
    support_pct                  # kind=if 만
    assumptions[]
    conditions[]                 # {metric, op, threshold, label}
    status                       # aligned|watching|diverging|n/a
    current{}                    # 평가에 쓴 현재값
    sector_keys[], sectors[], evidence[]
    children[]
```

캐시: `kv` 키 `hypo:v2:latest` (v1 카드형과 분리).

### 5.2 Layer0 지지도

| 성분     | 비중  | 입력                                      |
| ------ | --- | --------------------------------------- |
| 지표 일치  | 0.5 | regime / macro / 관련 지수 방향이 가정과 맞는지      |
| KB 근거  | 0.3 | 매칭 문서 수·신선도                            |
| 사이클 정합 | 0.2 | `cycle.position` 주도섹터와 `sector_keys` 겹침 |

### 5.3 템플릿 (큐레이션 IF → then → outcome)

1. **AI·CAPEX 지속** → then(나스닥↑·VIX 진정) / but(VIX↑·거시 비우호) → 섹터 outcome  
2. **정책·물가·소비 쪽 이동** → then(CPI 안정·금리↓) / but(물가 재가속)  
3. **리스크오프** → then(VIX↑·약세/조정) / and(나스닥↓ 동반)

부모 하나 아래 세 IF, 지지도 합 100%. 세 IF는 **동시에 성립한다고 읽히면 안 됨**.

## 6. API


| 메서드  | 경로                        | 역할                                              |
| ---- | ------------------------- | ----------------------------------------------- |
| GET  | `/api/hypothesis`         | 현재 트리(캐시). `{ ready, as_of, tree, disclaimer }` |
| POST | `/api/hypothesis/refresh` | 수동 재점수 — **관리자만**                                |


일일 훅: `_daily_kb_collect` 성공 후 `hypothesis.refresh()`.

## 7. 비목표

- Opus/LLM으로 가지 자동 생성  
- 시그널·봇 점수 반영  
- then 노드에 확률 %  
- 장중 실시간 스트리밍 갱신  
- `#9` 포트폴리오 MC와 통합  

## 8. 단계


| 단계 | 범위 |
| --- | --- |
| P0 (구) | 3갈래 카드 + HTML 플로우 |
| **P0.1 (본 재설계)** | IF/then/outcome 트리 + ECharts tree + status + kv v2 |
| P1 | 조건 게이지 강화, KB url 필드 정리 |
| P2 | 가지 추가·가중 슬라이더 |

## 9. 확정

- 서브탭: 인사이트  
- refresh: 관리자만  
- 다이어그램: ECharts tree  
- 생성: 룰+큐레이션 (LLM 없음)  
