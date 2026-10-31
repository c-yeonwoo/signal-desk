# 금융 분석 스킬 적용과 수익성 비교 준비

## 적용 범위

금융 스킬을 원천 확인·판단 근거 추적·짧은 해설에 적용한다. 기존 엔진의 후보 선정,
가중치, 문턱, 매수 보류, 손절·익절이나 등록 실험은 이 변경으로 달라지지 않는다.
로컬 분석 도구 설치가 Railway에 새 매매 엔진을 배포했다는 뜻은 아니다.

| 설치 구성 | 고정한 원천 | 적용 |
|---|---|---|
| thesis-tracker·catalyst-calendar | anthropics/financial-services `60565064557ca77b12f62aa7877f365d111c129d` | 판단 근거의 유지·훼손과 사건 일정 |
| stock-liquidity·earnings-recap·estimate-analysis | himself65/finance-skills `a0850786e72aa23b92862f9f1635b0de506fe206` | 체결 부담, 실적·가이던스·추정치 변화 |
| Alpaca MCP 2.4.1 | alpacahq/alpaca-mcp-server `d8dac43f3e17594e8879bac3b5b333196ea69aac` | 미국 주식 데이터만 조회 |
| signal-desk-research | 저장소 `.agents/skills/` | 위 절차를 기존 입력·잠금에 맞춰 연결 |

외부 스킬은 로컬 Codex skills에 설치하며 다음 턴부터 발견할 수 있다. 원천 저장소의
기본 Yahoo 수집·패키지 자동 설치·임의의 매매 기본값은 서비스에 이식하지 않는다.
새 분석은 프로젝트 스킬을 통해 이미 보존된 자료부터 사용한다. 새 역할 토론 서버나
Paperclip 회사·스케줄러는 만들지 않는다.

## 실제 연결부

`signals.research_packet.build`는 기존 다섯 아티팩트의 해시·참조를 확인한 뒤 다음을
묶는다. 엔진·LLM을 재실행하거나 DB·계좌를 읽지 않는다.

- 판단 ID, 관측 ID, 시세 원문·서버 수신 시각과 지정한 분석 시각.
- 기존 점수·등급·순위·차단 사유와 실제 양/음 점수 기여. 표시용 팩터와 구분한다.
- 전일 종가 대비 변화, 기존 가격 유효성 검사, 실적 일정의 시각 인증 여부.
- 아직 없는 연속 분봉·거래량·호가 수량·비용 근거. 현재가만으로 유동성을 추정하지 않는다.

CLI는 `python -m scripts.measure.finance_research_packet --bundle ... --as-of ... --output ...`다.
분석 시각은 명시해야 한다. 현재가 수신 시각을 원판단 생성 시각으로 바꾸지 않는다.
출력은 연구 입력이며 `live_order_eligible=false`, `profitability_verified=false`를 유지한다.
파일을 덮어쓰지 않으며, 원본 입력이나 원래 판단을 변경하지 않는다.

## Alpaca 연결

공식 패키지는 서비스 `.venv`와 분리한 `uv tool` 환경에 설치한다. 아래 실행기에 기존
로컬 `.env` 경로를 전달한다. 키를 MCP 설정·문서·출력에 복사하지 않는다.

```sh
uv tool install --python 3.12 \
  --from git+https://github.com/alpacahq/alpaca-mcp-server@d8dac43f3e17594e8879bac3b5b333196ea69aac \
  alpaca-mcp-server
```

`scripts/finance_tools/alpaca_readonly.py`는 설치 커밋을 확인하고 기존 `APCA_*` 키 이름을
공식 서버의 `ALPACA_*` 이름으로 메모리 안에서 연결한다. 관계없는 자격증명·프록시·API
호스트 덮어쓰기를 전달하지 않는다. `stock-data` 그룹을 고정하고 서버에 다시 정확한
여덟 개 도구의 허용 목록을 적용한다. 공식 서버는 이 그룹에서도 암호화폐 편의 도구와
암호화폐를 선택할 수 있는 범용 movers를 등록하므로 이들은 추가로 제외한다.

`--inspect`로 목록을 확인할 수 있으며 계좌·주문 요청은 하지 않는다. 일반 실행은 로컬
stdio 연결이다. 브라우저나 Railway 관리 연결이 필요하지 않다. 무료·실시간·과거 SIP
접근 권한은 계정의 실제 데이터 권한에 따르며 MCP 설치가 권한을 늘리지는 않는다.

## 다음 수익성 검증의 고정 계약

이 변경은 새 전략을 채택하거나 끝난 ORB·뉴스 필터 연구를 재개하는 명세가 아니다.
스킬이 보강한 설명만으로 주문이 같다면 성과 비교에서도 같은 경로여야 한다.

1. **검증할 변화 하나:** 후보의 결정 함수·입력 필드·버전/커밋을 고정한다. LLM을 쓰면
   제공자·모델·프롬프트·원문·출력을 보존하고, 결과를 본 뒤 다시 질의해 좋은 답을 고르지 않는다.
2. **같은 기회 집합:** 당시 거래 가능한 유동성 높은 종목과 원천 가용 시각을 고정한다.
   국내·미국은 별도 평가한다. 보호 표본과 기존 연구 창을 임의로 개발 자료로 바꾸지 않는다.
3. **같은 실행 조건:** 동일 초기자본, 정수 수량, 위험 예산, 주문 지연·스프레드·체결 가능한
   잔량·미체결·청산 규칙·계좌 비용을 사용한다. 손절가를 보장 체결가로 취급하지 않는다.
4. **대조:** 기존 운영 정책, 같은 모집단의 단순 기준, 현금 경로를 비교한다. 노출 감소에 따른
   손실 축소와 선정력 개선을 구분한다. 비용 후 손익·낙폭·체결률·종목 집중을 함께 낸다.
5. **학습과 판정:** 개발 자료로 후보를 고른 뒤 별도 평가 구간을 한 번 연다. 기간·자료
   요건과 채택 기준은 결과 열람 전에 커밋한다. 원천 부족·미체결을 0원 수익으로 채우지 않는다.

현재 판단 묶음에는 거래 가능한 bid/ask, 연속 거래량, 실제 계좌 비용, 새 후보 정책과
독립적인 성과 표본이 모두 들어 있지 않다. 패킷의 `comparison_readiness.missing`이 이
부족분을 명시한다. 설치 완료를 백테스트 실행 가능 또는 수익성 입증으로 세지 않는다.

## 원천·라이선스

- [Anthropic 금융 스킬](https://github.com/anthropics/financial-services): Apache-2.0.
- [Finance Skills](https://github.com/himself65/finance-skills): MIT.
- [Alpaca 공식 MCP](https://github.com/alpacahq/alpaca-mcp-server): MIT.
- [OpenAI MCP 설정](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

이 저장소의 연결 코드·프로젝트 스킬은 새로 작성했다. 외부 코드·스킬의 원문은 원래
설치 위치에 유지하고 라이선스와 고정 커밋은 설치 증거에 함께 보존한다.
