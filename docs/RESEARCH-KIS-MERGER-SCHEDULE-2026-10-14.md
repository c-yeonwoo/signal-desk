# 합병 일정 원문 교차 검수: 날짜와 교부비율을 분리

2026-10-14 사후 조회·연구 전용. KIS의 [공식 합병·분할 일정 샘플](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/ksdinfo_merger_split/ksdinfo_merger_split.py)에 맞춰 기존 보존 수집기를 확장했다. 로컬 `demo` 계정에서 각 종목·달력월을 읽기 전용으로 한 번씩 조회했다. 모든 응답은 `rt_cd=0`, 한 페이지 종료였고 원문·관측시각·해시를 로컬 `data/research/` ZIP에 보존했다. 조회 시각은 당시 투자자가 알 수 있었던 시각이 아니다.

| 구주·조회월 | KIS 일정 | 기존 발행사 원문과의 관계 | 보존 ZIP SHA-256 |
| --- | --- | --- | --- |
| HD현대미포 `010620`, 2025-11 | 기준일 `11-28`, 거래정지 `11-27~12-14`, 신주 상장 예정 `12-15`, 단주대금 지급일 필드 `12-30`, `merge_rate=0.41` | [합병종료보고](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20251201000400)의 효력일 `12-01`·교부조건 `0.4059146`을 대체하지 않음. KIS 비율은 두 자리 표시라 실제 교부 수량에 사용 불가 | `aa30830c70d854c3c84893c5fe9472767d54a2b37c7c3fb2d767b878796d8931` |
| HD현대인프라코어 `042670`, 2025-12 | 기준일 `12-31`, 거래정지 `12-29~01-25`, 신주 상장 예정 `01-26`, 단주대금 지급일 필드 `02-02`, `merge_rate=0.16` | [합병종료보고](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260102000224)의 효력일 `2026-01-01`·표시 교부조건 `0.1621707`을 대체하지 않음. 발행사 원문조차 정확 비율 정밀도가 미해결 | `0d169deff3e766ccc486420d2765b2d7d8bb5815de4d4322341c710716dc24a2` |

KIS `odd_amt_pay_dt`는 **일정 필드**다. 실제 단주 현금 지급·개인 계좌 입금 또는 그날 주문 가능 현금을 증명하지 않는다. `merge_rate`를 더 정확한 원문의 계약 비율 대신 쓰지 않는다. 앞뒤 달의 동일 조회가 빈 응답인 점은 이 API의 검색일 기준을 추정하는 단서일 뿐, 전체 역사 범위·모든 사건의 부재를 증명하지 않는다.

두 양성 대조 밖의 응답은 `010620` 2025-10·12/2026-01, `042670` 2026-01, `000660` 2024-06 합병·분할 및 2026-02 자본감소에서 모두 0행이었다. 특히 SK하이닉스의 2026-02 자기주식 소각을 일반 주주의 자본감소 일정으로 억지 분류하지 않는다. 이 빈 검색도 [기존 KIND 자기주식 검수](RESEARCH-TRADED-ACTION-SCOPE-2026-10-13.md)의 보유수량 불변 사실을 바꾸지 않는다.

추가 원천의 공식 KIS 경로는 [무상증자](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/ksdinfo_bonus_issue/ksdinfo_bonus_issue.py), [자본감소](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/ksdinfo_cap_dcrs/ksdinfo_cap_dcrs.py), [유상증자](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/ksdinfo_paidin_capin/ksdinfo_paidin_capin.py), [상장정보](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/ksdinfo_list_info/ksdinfo_list_info.py)다. 수집 성공은 여전히 `query_date_basis=unknown`, `historical_range_confirmed=false`, `coverage_state=unknown`으로 보고한다. 다섯 유형은 원문 보존만 하고 `ActionRecord`로 자동 정규화하지 않는다. KSD 계열 일정 하나로 행사 전수성·정정 사슬·효력일·입금을 인증할 수 없기 때문이다.

재현: 각 ZIP에 `acquire_historical_sources.py verify-actions --output <ZIP>`을 실행하면 원문 바이트와 해시가 재검사된다. 두 회사의 발행사 원문 교부조건은 [동결된 합병 감사](research-share-exchange-source-audit-2026-10-12.json)와 [정산 결정](RESEARCH-SETTLEMENT-DECISIONS-2026-10-13.md)을 참조한다. ZIP은 키가 없는 로컬 연구 원문이라 Git에 올리지 않는다. **모의 권리의 날짜 비교에는 일정 후보로 유용하지만, 7개 창의 기간 총수익·strict PIT·실전 주문을 열 수 없다.** 후보 1,488행의 분모와 통합 진행률 **40/100** 유지.
