# Desk Hierarchy — 권한 사다리 (정본)

> 합의(2026-07-31): 에이전트 부서 조직도가 아니라 **권한 위계**다.  
> 위로 갈수록 “더 많이 말하는” 게 아니라 **더 신중히 막거나 기권**한다.  
> 관련: [verification-system.md](verification-system.md) · [kb-decision-architecture.md](kb-decision-architecture.md) · [selection-and-exposure.md](selection-and-exposure.md)

## 목표 · 비목표

**목표**
- 매수/매도 판단이 어떤 층을 거쳤는지 한 장으로 추적 가능하게 한다.
- 빈 매수일·공석·기권을 **고장**이 아니라 **정밀도 작동**으로 보이게 한다.
- LLM은 후보 **안**에서 제거·기권·설명만 하고, 점수·kind·주문은 코드가 사수한다.

**비목표**
- TradingAgents식 전 종목 다층 LLM 토론으로 엔진을 교체.
- “매일 수익”을 성공 지표로 두기 (북극성은 D7, 알파는 IC·shadow).
- LLM끼리 합의로 kill switch / 문턱 / 비중을 바꾸기.

## 사다리

```text
L0  Quant Desk     전 종목 · 팩터·게이트·rank 매수권·공석     [코드 · 점수/kind]
L1  Fact Cards     매수권만 · 이미 계산된 필드 조립            [코드 · 설명]
L2  Committee      Bull 선별 → Bear 제거만 (현 advisor)       [LLM · 후보 안]
L3  Risk Clerk     exposure · 편중 · 이벤트 · vol sizing       [코드 · 크기/기권]
L4  Desk Report    오늘 한 장 보고 (왜 샀/안 샀/공석)         [템플릿 · 불변]
L5  Execution      손절·익절·정수주·슬롯                      [코드 · 절대 사수]
∥
Lx  Product Review 제품·데이터 공백 지적 (매매 경로 밖)       [LLM · BACKLOG만]
∥
Lv  Verdict        shadow / harness / 레드팀                  [기계 · 경로 on/off]
```

| 층 | 입력 | 출력 | 점수·kind | 주문 |
|---|---|---|---|---|
| L0 | 시세·재무·KB 이벤트 | SignalResult · selection | **쓴다** | 불가 |
| L1 | L0 필드 | 카드(템플릿) | 불변 | 불가 |
| L2 | 매수권 후보 | picks / [] / None | 불변 | 불가 |
| L3 | picks + 국면 | 사이즈·기권 | 불변 | 불가 |
| L4 | L0~L3 요약 | `desk_report` JSON | 불변 | 불가 |
| L5 | L3 지시 | paper 체결 | 불변 | **코드만** |
| Lx | 스냅샷 | 개선 후보(+falsifier) | 불변 | 불가 |
| Lv | 실현수익 | kill / 유지 | 경로만 | 불가 |

## 승격 규칙

1. **L2 세분화**(기술/펀더/시황 분리 호출)는 paired shadow가 유의하게 이긴 뒤에만.
2. **L1 LLM 브리프**는 L4 템플릿으로 부족할 때만 — 기본은 코드 조립.
3. **Lx** 지적은 `falsifier` 없으면 버린다. 엔진·봇에 자동 반영 금지.
4. 편중 경고에서 `미분류` 집중은 **섹터 crowded가 아니라 데이터 공백**으로 보고한다.

## 구현 상태

| 조각 | 상태 |
|---|---|
| L0~L3·L5·Lv (기존 엔진·advisor·bot·shadow) | ✅ 운영 중 |
| L4 Desk Report v1 (`signals/desk_report.py`) | ✅ 템플릿 |
| Lx Product Reviewer 스케치 | ✅ 모듈·문서 (자동 스케줄은 후속) |
| L1 역할 분리 LLM / L2 다라운드 토론 | ⬜ 승격제 — 보류 |
