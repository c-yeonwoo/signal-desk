# 개장 거래량·돌파 후보: 고정 대형주 패널 재구성

이 문서는 [장중 엔진 설계](ENGINE-INTRADAY-REBOOT-2026-10-25.md)의 원천 확보와
첫 실행 비교를 기록한다. 운영 엔진·등록 연구·실주문은 변경하지 않는다.
당시 전체 시장을 재현한 백테스트가 아니라 **고정 8종목의 조건부 재구성**이다.

## 성과 확인 전 고정한 조건

첫 계획은 `data/research/intraday-orb-plan-2026-10-25-v1.json`에 새 파일로 보존했다.
SHA-256은 `4420ea9d2783fa9222022fae3dad20aada9d619cb87c86b75be28b8e7e71e5d5`다.
진입·청산 손익을 보기 전에 기간·명단·비용 가정·비교·판정 코드를 고정했다.

| 항목 | 고정값과 의미 |
| --- | --- |
| 패널 | AAPL·AMZN·GOOGL·JPM·META·MSFT·NVDA·XOM. 사후 고정 생존 종목 패널이며 당시 시총100→거래대금50 유니버스가 아님 |
| 입력 | 2023-12-01부터 선행20세션 + 2024-01-02부터240세션. SIP/raw/USD, 정규장1분 OHLCV·VWAP |
| 분할 | 시간순120 개발 /60 검증 /60 봉인평가. 이 후보를 본 뒤 마지막 구간에서 재튜닝하지 않음 |
| 명단 | 개장5분 완료 후 RVOL≥1 중 상위2. 분모는 직전14세션 같은5분 거래량 평균. 음봉·미돌파가 있어도 명단 보충 없음 |
| 비교 | 같은 가격/유동성 조건·돌파·청산·위험한도의 직전20세션 거래대금 중위순 상위2, 현금0 |
| 진입 | 완료1분봉 종가의 첫 상향 돌파, 봉 종료+가정1초 이후 첫 정상 양방향 호가. 개장60분 전, 유효5초, 한 번의 의도 |
| 제한 | 전일종가≥$10, 갭≤5%, 첫5분범위≤2%, spread≤10bp, ask≤첫5분고가+범위0.25 |
| 수량 | 초기NAV $10,000, 정수주, 예정손실0.25%/회·원금20%/종목·40%/일 이내. 허용 최고 진입가로 비용 포함 수량 계산 |
| 청산 | 첫5분저가 손절·실제ask+2R·첫 체결 후60분·폐장5분 전 중 먼저. 완료 분봉을 본 뒤 실제bid로 청산하는 재구성 |
| 비용 | 스프레드는 ask/bid에 이미 포함. 별도 비용은 각 편27bp 가정, 스트레스54bp. 이 계좌 당시 실정산 인증값이 아님 |
| 스트레스 | 비용2배, 주문 제출+60초, 호가 표시수량50%. 유리한 경로를 본 뒤 기본값을 바꾸지 않음 |

이것은 미래 자료로 학습한 ML 모델이 아니다. 기간 분할은 정해진 단일 규칙의 시간 안정성과
평가 열람을 분리하기 위한 것이다. 현재 날짜의 전체 거래량·고가·저가를 개장 명단에 넣지 않는다.

## 무엇을 실제로 재사용했나

- `market_clock`: 미국 DST·휴장·조기 마감. 정규장 예정390분 또는 조기 마감210분을 전부 대조한다.
- `ingest.alpaca_research`: 기존 read-only SIP 파서·원문 바이트·요청·시각·해시·행수·페이지 상태.
- `broker.execution.calculate`: 정수 수량의 buy/sell 현금·비용 계산. 가상 종가 체결을 새로 만들지 않는다.
- 새 두 스크립트만 로컬 연구를 조율한다. 운영 DB·가격 갱신기·LLM·주문 API는 호출하지 않는다.

수집기는 기본 직렬 또는 최대4개 병렬 요청이며, 전체 요청 시작 간격은 최소0.5초다.
명시한 요청 예산 이내에서만 실행하고, 실패/부분 응답이 나오면 새 요청을 멈춘다.
진행 중 응답도 보존하며 원문 파일을 원자적으로 새로 발행한다. 기존 파일은 해시·요청을
검증해 재사용한다. 키는 환경에서 읽으며 원문·manifest·로그에 넣지 않는다.

분봉은 정규장 `[open, close)`의 모든 분을 요구한다. 가격·거래량 구멍을 보간하거나 해당 날짜를
몰래 제외하지 않는다. 호가 TTL은 `[주문 가능시각, +5초)`이며 정상 조건 `R`, 양수 bid/ask/size,
양쪽 거래소, bid<ask를 확인한다. 요청은 공급자 포함형 end 때문에 끝 행을 받을 수 있지만
정확히 만료 시각의 행은 체결하지 않는다.

청산 관측이 없거나 bid 잔량이 수량보다 적으면 이미 열린 포지션을 현금0%로 바꾸지 않는다.
해당 계좌 전체가 불완전이고 총수익은 null이다. 부분 페이지의 검증된 앞부분에서 최초 정상
호가가 나오면 그 행은 사용할 수 있지만, 뒤를 못 본 상태를 미체결 증거로 쓰지 않는다.

## 해석 한계와 승격 금지

1. **전체 시장 선정력 아님.** 당시 시총·상장 상태·대형주 전체 구성이 없다. 알려진8종목의
   결과를 미국 전체의 후보 발견 성능으로 읽지 않는다. 국내 성능으로 옮기지도 않는다.
2. **실제 수신·대기열 아님.** 공급자의 현재 과거 SIP 원문이며 정정 이력·그날 API 수신은
   보존하지 않았다. 1초 도착 가정과 +60초 스트레스는 수신 능력의 실측이 아니다.
3. **과거 수량 단위 미인증.** [Alpaca 공지](https://docs.alpaca.markets/us/v1.1/changelog/marketdata-bid-and-ask-size-display-change)는
   2025-11-03 전 round-lot, 이후 shares 표시 전환을 설명한다. 현재 재조회한2024년 원문을
   소급 변환했는지는 별도 인증하지 않았다. 기본 경로는 원시 표시수량1을1주로 제한한다.
   보수적 한도 가정이지 정확한 과거 유동성 인증이 아니며 수량100배를 보고 승자를 고르지 않는다.
4. **당시 계좌 비용 아님.** 현재 [한국투자증권 미국 온라인 표준 수수료](https://securities.koreainvestment.com/main/bond/research/_static/TF03ca050000.jsp)
   25bp에 추가2bp를 둔 가정이다. 실제 계좌의 과거 매수·매도 수수료/세금/체결 보존을 대신하지 않는다.
5. **분봉 청산.** tick-level stop/target 체결이 아니다. 같은 봉에서 둘 다 닿으면 손절 우선,
   진입 봉의 유리한 target은 무시하며 모호성을 원장에 남긴다. 모호성이 있는 봉인 결과는 통과하지 않는다.
6. **기업행사 범위 미인증.** raw 가격을 쓰며 gap 제한으로 일부 큰 단위 변경일이 제외될 수 있지만,
   이것이 분할 후 거래량 단위·배당/정지까지 인증하지 않는다. 기업행사 전수 조사도 재개하지 않는다.

원천 인증 네 항목(`pit_largecap_universe`, `account_cost_vintage`,
`historical_quote_size_units`, `security_events`)은 모두 false다. 조건부 비교가 좋아도
`not_adopted_source_incomplete`이며 `live_order_eligible=false`다. 어느 플래그도 손익을 보고
자동으로 true로 바꾸지 않는다. 원천 결손·성능 실패·도구 완료는 별개다.

봉인60세션에서 비용 후 양수, 거래대금 대조 우위, 일별 초과의5세션 원형블록10,000회
bootstrap 단측97.5% 하한>0, 더 나쁘지 않은 일별 변동성/낙폭, 세 스트레스 양수,
최고 기여 종목/날짜 각각 제외 후 양수, 모호성0을 조건부 검사한다. 일별 변동성이 같다는
위험 인증은 아니며 장중 최대낙폭도 아니다. 원래 설계의 개발 기반 위험 정렬·최소효과/검정력
확정까지 완료한 실전 승격 시험으로 설명하지 않는다.

## 실행

원문은 ignored `data/research/`의 로컬 파일이다. Railway Volume에 이 연구 자료를 적재하지 않는다.
모든 출력은 새 이름만 허용한다. 원천 수집 뒤 `needs`로 아직 없는 호가만 계획하고,
진입 호가 보존 뒤에는 새로 결정된 청산 호가를 추가한다. 같은 원문으로 수익률을 반복 탐색하지 않는다.

```bash
.venv/bin/python -m scripts.measure.intraday_orb_replay freeze --plan data/research/<새 계획>.json
.venv/bin/python -m scripts.measure.intraday_orb_sources --plan data/research/<계획>.json \
  --outdir data/research/<원천 폴더> --max-requests 2400 --workers 4
.venv/bin/python -m scripts.measure.intraday_orb_replay needs --plan data/research/<계획>.json \
  --sources data/research/<원천 폴더> --output data/research/<새 호가 계획>.json
.venv/bin/python -m scripts.measure.intraday_orb_sources --quote-plan data/research/<호가 계획>.json \
  --outdir data/research/<원천 폴더> --max-requests 1000 --workers 4
# 새로 결정된 청산 needs를 같은 방식으로 수집한 뒤:
.venv/bin/python -m scripts.measure.intraday_orb_replay compare --plan data/research/<계획>.json \
  --sources data/research/<원천 폴더> --output data/research/<새 비교 결과>.json
```

## 실제 결과

현재 원천 수집 중이다. 완료된 결과나 수익성 개선으로 세지 않는다. 결과는 원문 해시·실제
호출 수·결손·각 경로와 함께 아래에 추가하며, 운영 매수 보류는 그대로 유지한다.
