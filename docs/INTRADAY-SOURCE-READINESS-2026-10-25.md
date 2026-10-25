# 장중 원천 연결 점검 · 2026-10-25

대상은 [장중 리부트](ENGINE-INTRADAY-REBOOT-2026-10-25.md)의 R1이다. 원천 연결과
전략 수익성은 별개이며, 이 문서는 새 단타 정책의 채택 보고가 아니다.

## 실제 접근 확인

운영 관리자 경로에서 기존 Railway Alpaca 키의 설정 유무를 확인했다. 키는 출력·복사하지 않았다.
고정 종목 AAPL의 2024-06-03 13:35:00–13:35:10 UTC SIP 호가 한 구간만 요청했다.

- HTTP 200, `status=observed`, 591행, 다음 페이지 없음.
- `unknown_condition_rows=0`. 이것만으로 모든 행의 정상 NBBO·체결 가능성·당시 가용성을 인증하지 않는다.
- 원문 SHA-256: `937db69da21ce50d8bd027d4bee5e193d2fce37b1e2839a9e49529e4cce8ed46`.
- 과거 SIP 접근 성공이지 최신 SIP 권한·실시간 전체 시장 접근의 증명이 아니다.
- 유료 구매·자동 수집·새 계좌·실주문·성과 계산은 하지 않았다.

국내 로컬 KIS 설정은 demo이며 일치하는 토큰도 만료 상태였다. 이것을 운영 Railway의 설정으로
간주하지 않고 real로 임의 전환하지 않았다. 국내 실제 접근은 배포 후 기존 관리자 진단에서 확인한다.

## 이번 구현

| 원천 | 변경 | 유지하는 제한 |
| --- | --- | --- |
| KIS 과거 분봉 | 원문 시가·고가·저가·종가와 요청 시장 `J`, `ohlc_status` 보존 | real 전용, 1년 조회 범위, 2026-08-04 이전 개발 구간, 기존 페이지 제한 |
| Alpaca 과거 분봉 | SIP/raw/1Min 한 페이지와 정확한 원문·해시 보존 | 기존 고정 10개 심볼, 2020–2025년, 같은 NY 날짜, 최대 7시간·1000행·512KB |
| 관리자 GET | `/api/admin/research/alpaca-bars` | 인증 먼저, 입력 검증 뒤 기존 호가/뉴스와 공유하는 초당 1회 lease, 자동 반복 없음 |

외부 조회 구간은 `[start,end)`다. Alpaca의 포함형 end에는 요청 끝에서 1초 뺀 값을 넘기고,
반환 분봉 시각을 다시 원래 구간 안에서 검증한다. OHLC의 양수·유한성·범위, 분 단위 시각,
중복·순서·거래량을 검사한다. 날짜 간 비교에 보정 가격을 몰래 섞지 않는다.

국내 기존 `bars`의 `time/price/volume/date_reported`와 해시는 그대로 보존하고 새 OHLC는
별도 `ohlc_bars`·해시로 반환한다. 표현 필드 추가를 실제 가격 개정으로 오인해 기존 재생이
막히지 않게 한다. OHLC가 없으면 `missing`, 잘못됐으면 `invalid`로 두고 종가를 시가·고가·저가로
복사하지 않는다. `J`는 요청 시장이지 KRX/NXT 합산 인증이 아니다. 이미 저장한 close-only
원문을 새 OHLC 자료라고 재분류하지 않는다. 기존 ID에 후속 OHLC가 소급 추가되는 것도 아니다.
신규 OHLC는 이번 진단 응답·캐시에서 확인하며 별도 장기 원문 아카이브 완료로 세지 않는다.

`response_complete`는 다음 페이지가 없다는 의미뿐이다. 거래소 휴장·조기 마감·연속 분봉
충족 여부는 별도로 확인해야 한다. 응답은 `strict_pit_eligible=false`, `live_order_eligible=false`,
`coverage_status=session_and_continuity_unverified`를 유지한다. 빈 봉 보간·IEX 폴백은 없다.

## 다음 관문

1. 배포된 미국 경로에서 AAPL 2024-06-03 13:30–13:35 UTC 5분 구간의 실제 원문을 확인한다.
2. 국내 기존 보호 전 진단에서 OHLC 제공 여부·venue·날짜 시각을 확인한다. 로컬 토큰을 우회하지 않는다.
3. 동일 feed의 14개 선행 세션·당시 대형주/거래대금 모집단·매수/매도 호가와 실제 비용이
   충족되는 표본만 R2/R3에 넘긴다. 현재 확보 자료만으로 이 관문이 충족됐다고 하지 않는다.

R1 **부분 구현**, R2 정책 구현·R3 비용 후 백테스트·R4 채택은 미완료다. 원천 확인을
위해 운영 매수 보류를 풀지 않는다. 통합 로드맵 **40/100 유지**.

공식 계약: [Alpaca 과거 분봉](https://docs.alpaca.markets/us/reference/stockbars),
[Alpaca 시장 데이터](https://docs.alpaca.markets/us/docs/about-market-data-api),
[KIS 과거 분봉 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_time_dailychartprice/inquire_time_dailychartprice.py).
