# R3 보유 갱신·재진입 후보 실행 결과

**판정: 자료 제약으로 채택하지 않는다. 수익성 기각도, 0% 수익도 아니다.** 구현·사전 계약·1회 실행은 수행했지만 실제 체결0건이어서 후보의 손익과 타이밍 효과를 검증하지 못했다. 운영 매매는 변경하지 않는다.

Paperclip [SIG-10](http://127.0.0.1:3100/SIG/issues/SIG-10)에서 정량/체결 제안과 QA 반박을 거쳐 `kr-r3-h5-ma20-renew-cool5-v1`을 선택했다. [사전 계약](KR-R3-HOLD-RENEWAL-CONTRACT-2026-10-18.md)은 `1667e997510644e1fbcce65cd03ad8119ee33031`에 결과를 보기 전에 커밋했다. 20일 평균 이상 보유 갱신·청산 유지·실제 전량 매도 후5세션 재진입을 구현했다.

## 실제 확인한 것

- 35개 고정 입력/코드의 바이트 해시와 계약 사전 커밋을 대조했다.
- 2024Q4의61일·11,644예측 행과 원래 선정·순위를 재현했다. 동결 모델로2025Q1의58일·11,223예측 행을 생성했다. 재학습·새 수집·모델 튜닝은0이다.
- 세 거래팔×두 비용(75/120bp)×5위상×두 기간=60계좌와 현금선2개를 준비하고1회 실행했다. 경과8.856초·CPU8.458초였다. **완결 거래 경로0, 실제 모의 체결0**이다. 숫자62는 완료된 백테스트 수나 독립 표본 수가 아니다.
- 모든 거래팔은 각 위상 최초 판단일의 입력 검사에서 중단됐다. 세 팔 최종 가격손익·비용·MDD·쌍별 차이는null이다. 시작 전 현금 평가액을 전략의0% 성과로 쓰지 않는다.

## 최초 차단 근거

현대차(`005380`)의 KRX 상장주식수가2024-04-15의211,531,506주에서04-16의209,416,191주로 바뀐다. 기존8건의 검수된 수량불변 사실에는 이 사건이 없다. 이번 계약은 매매 당일뿐 아니라 모델/MA20이 실제 사용하는 과거 단위 문제도 검사하므로 모든 위상에서 이 최초 미검수 사건을 발견했다. 문제의 과거 날짜와 현재 판단/중단 날짜는 별도로 기록한다.

**이것만으로 주가가 틀렸다거나 실제 보유수량이 변했다고 단정하지 않는다.** 상장주식수 변화의 종류·가격 단위·보유자 영향이 확인되지 않은 상태다. 해당 종목은 시장 대표값과 순위 특성에도 사용되므로 선정되지 않았다는 이유로 조용히 지우지 않았다. 앞선 SIG-8의 매매 인접일 검사보다 입력 이력 검사가 넓어졌다는 점도 결과 차이의 원인이다.

이미 보존한7건의 검수 사실을 제한적으로 대조했지만 이 현대차 사건의 증거는 없었다. 이번에는 수집·유료 문의·같은 자료 재실행·문턱 완화·다른 후보 실행을 하지 않았다. 이 최초 결손을 해결하면 다른 결손이 없어진다는 보장도 없다. 다음 자료 작업은 **해당 사건의 원문으로 실제 가격 단위/보유자 영향을 구분하는 한 건**이며, 신규 거래일 관측을 기다리는 문제가 아니다.

75bp의 경로별 상태는 아래와 같다.120bp도 같은 최초 사건에서 중단됐다. ‘공통 마감’은 매수 전 현금 상태를 확인한 날일 뿐 성과 관측일이 아니다.

| 기간 | 위상 | 발견·중단일 | 마지막 공통 마감 | 체결(세 팔) | 후보 가격손익 |
| --- | ---: | --- | --- | ---: | --- |
| 2024Q4 개발 | 0 | 2024-10-02 | 없음 | 0 | 미계산 |
| 2024Q4 개발 | 1 | 2024-10-04 | 2024-10-02 | 0 | 미계산 |
| 2024Q4 개발 | 2 | 2024-10-07 | 2024-10-04 | 0 | 미계산 |
| 2024Q4 개발 | 3 | 2024-10-08 | 2024-10-07 | 0 | 미계산 |
| 2024Q4 개발 | 4 | 2024-10-10 | 2024-10-08 | 0 | 미계산 |
| 2025Q1 평가 | 0 | 2025-01-02 | 없음 | 0 | 미계산 |
| 2025Q1 평가 | 1 | 2025-01-03 | 2025-01-02 | 0 | 미계산 |
| 2025Q1 평가 | 2 | 2025-01-06 | 2025-01-03 | 0 | 미계산 |
| 2025Q1 평가 | 3 | 2025-01-07 | 2025-01-06 | 0 | 미계산 |
| 2025Q1 평가 | 4 | 2025-01-08 | 2025-01-07 | 0 | 미계산 |

## 구현·검수와 채택 판단

구현 담당 [SIG-15](http://127.0.0.1:3100/SIG/issues/SIG-15)의 staged 보고 개정은 `86dccc1f-1425-4bdf-82a7-8c2cc8ccccc6`이다. controller는 독립 코드리뷰에서 발견한 과거 메타 완전성 과잉 요구, 중단 시각 역행, 이미 관측한 낙폭 위반의 소실을 수정했다. 테스트의 로컬 절대 경로도 제거했다. 핵심 합성 경계15개와 독립 코드리뷰를 통과했으며 합성 검사가 실제 후보 수익을 입증한 것은 아니다. 실제 결과의 독립 판단은 [SIG-16](http://127.0.0.1:3100/SIG/issues/SIG-16)에 연결한다. 전체 CI/머지는 PR의 최종 커밋 상태로 확인한다.

`limitedDevelopmentPriceSupport=false`이고 역사 연구 채택/실전 승격도false다. 수익 손실이 관측돼 경제적으로 기각한 것이 아니라 비교 가능한 거래가 없어 지지를 판단하지 못했다. 같은 후보를 조건만 바꿔 합격시키지 않는다. 기존 R5/210경로와 등록 조건은 그대로이며 통합40/100을 유지한다.

SIG-16은 보고 개정 `c0503b6b-529c-4552-8ce5-5e5c6dc7e01a`에서 **도구 병합 가능 / 자료 제약으로 전략 채택 불가**로 판단했다. 실제 작성자·실행·서버activity를 대조했다. QA는 보존 KRX 원문/manifest와 주식수 변동·기존8사건의 부재, 20그룹의 제출/체결/완결0과 모든 최종금액null을 확인했다. `price_diagnostic_finished`와 경과시간은 절차 종료이지 수익 백테스트 완결이 아니라고 재확인했다. 추가 P1/P2는 없었으며 테스트·역사 실행은 반복하지 않았다.

## 재현과 보존

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m scripts.measure.kr_ridge_hold_renewal_diagnostic \
  --repo /Users/ys.choi/dev-private/signal-desk \
  --output /Users/ys.choi/dev-private/signal-desk/data/research/kr-ridge-hold-renewal-diagnostic-2026-10-18-v1.json
```

실행기는 기존 출력 덮어쓰기를 거절한다. 같은 입력·같은 결손을 다시 돌리지 않는다. 완전한 로컬 원장·의도·trace는 아래 artifact에 있고, Git에는 경로별 원장 요약·null 사유와 해시를 남긴다. 원문 ZIP이나 전체 가격 행은 커밋하지 않는다.

```json
{
  "artifact": {
    "path": "data/research/kr-ridge-hold-renewal-diagnostic-2026-10-18-v1.json",
    "sha256": "ad7bd5f5a4719e212ca4bc0dc39527d0870a655aaa3cb74829fbdc2ff5d201c4",
    "bytes": 2493763
  },
  "state": "price_diagnostic_finished",
  "contract_commit": "1667e997510644e1fbcce65cd03ad8119ee33031",
  "contract_sha256": "13ce5fbdfec1f41583e8ced2fd2d4d6c9f55db994bda3619ee888d2a152884bd",
  "code_sha256": {
    "runner": "bf6111d2b132f516ea13bb15c8a65efcffce2df71d051618831550a43bae92d7",
    "staged_tests": "30c09789b8c9fd7776c554f5cde7bf9af3ba898b8e14b3df929933afbe5ea44e"
  },
  "elapsed_seconds": {
    "wall": 8.85606512503,
    "cpu": 8.458166
  },
  "Q4_reproduction": "all_prediction_rows_selection_rank_matched",
  "predictionSummary": {
    "dev_2024q4": {
      "dates": 61,
      "rows": 11644
    },
    "historical_price_2025q1": {
      "dates": 58,
      "rows": 11223
    }
  },
  "tradingPathCount": 60,
  "cashSeries": 2,
  "decision": "not_adopted_source_constraint",
  "economic_rejection": false,
  "gates": {
    "scope": "historical_price_2025q1",
    "observedGateFailures": [],
    "minimumNotMet": [],
    "incompleteTradingPaths": 30,
    "implementationFailure": [],
    "limitedDevelopmentPriceSupport": false,
    "adoptionClaim": false,
    "historicalResearchAdoption": false,
    "livePromotion": false
  },
  "groups": [
    {"period":"dev_2024q4","cost":75,"phase":0,"stop":{"session":"2024-10-02","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2024-10-02:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":null,"ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"dev_2024q4","cost":75,"phase":1,"stop":{"session":"2024-10-04","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2024-10-04:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2024-10-02","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"dev_2024q4","cost":75,"phase":2,"stop":{"session":"2024-10-07","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2024-10-07:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2024-10-04","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"dev_2024q4","cost":75,"phase":3,"stop":{"session":"2024-10-08","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2024-10-08:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2024-10-07","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"dev_2024q4","cost":75,"phase":4,"stop":{"session":"2024-10-10","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2024-10-10:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2024-10-08","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"dev_2024q4","cost":120,"phase":0,"stop":{"session":"2024-10-02","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2024-10-02:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":null,"ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"dev_2024q4","cost":120,"phase":1,"stop":{"session":"2024-10-04","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2024-10-04:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2024-10-02","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"dev_2024q4","cost":120,"phase":2,"stop":{"session":"2024-10-07","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2024-10-07:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2024-10-04","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"dev_2024q4","cost":120,"phase":3,"stop":{"session":"2024-10-08","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2024-10-08:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2024-10-07","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"dev_2024q4","cost":120,"phase":4,"stop":{"session":"2024-10-10","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2024-10-10:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2024-10-08","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"historical_price_2025q1","cost":75,"phase":0,"stop":{"session":"2025-01-02","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2025-01-02:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":null,"ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"historical_price_2025q1","cost":75,"phase":1,"stop":{"session":"2025-01-03","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2025-01-03:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2025-01-02","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"historical_price_2025q1","cost":75,"phase":2,"stop":{"session":"2025-01-06","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2025-01-06:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2025-01-03","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"historical_price_2025q1","cost":75,"phase":3,"stop":{"session":"2025-01-07","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2025-01-07:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2025-01-06","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"historical_price_2025q1","cost":75,"phase":4,"stop":{"session":"2025-01-08","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2025-01-08:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2025-01-07","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"historical_price_2025q1","cost":120,"phase":0,"stop":{"session":"2025-01-02","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2025-01-02:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":null,"ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"historical_price_2025q1","cost":120,"phase":1,"stop":{"session":"2025-01-03","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2025-01-03:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2025-01-02","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"historical_price_2025q1","cost":120,"phase":2,"stop":{"session":"2025-01-06","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2025-01-06:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2025-01-03","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"historical_price_2025q1","cost":120,"phase":3,"stop":{"session":"2025-01-07","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2025-01-07:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2025-01-06","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}},
    {"period":"historical_price_2025q1","cost":120,"phase":4,"stop":{"session":"2025-01-08","ticker":"005380","field":"unreviewed_unit_or_rights_change","stage":"close","source_id":"preserved-raw:2024-04-16:005380","reason":"source_gap:2025-01-08:005380:unreviewed_unit_or_rights_change","evidence_session":"2024-04-16"},"last_common_close":"2025-01-07","ledger":{"forced":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"net":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0},"candidate":{"fills":0,"buy_principal":0,"sell_principal":0,"holdings":{},"pendingOrders":0,"unsettled":0}},"accountMetrics":{"forced":null,"net":null,"candidate":null},"deltas":{"candidate-forced":null,"candidate-net":null}}
  ]
}
```
