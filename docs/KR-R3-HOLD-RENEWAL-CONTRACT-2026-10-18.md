# R3 보유 갱신·재진입 후보 사전 계약

2026-10-18, 실제 후보 결과를 계산하기 전에 고정한다. Paperclip [SIG-10](http://127.0.0.1:3100/SIG/issues/SIG-10)의 정량 제안·체결 제안·QA 반박 뒤 [SIG-14](http://127.0.0.1:3100/SIG/issues/SIG-14)가 `kr-r3-h5-ma20-renew-cool5-v1` 하나를 선택했다. 근거 보고 개정은 `9a78959b-3d33-43ea-b2a7-b397a0d2b568`, `e0680d06-aa69-450f-a4a8-2f0692398dcd`, `0829b31d-26f6-4720-81cb-6d1719890889`, 최종 결정 개정은 `4f6bee39-6828-4679-97a7-50c613272b75`다.

기존 선정의 최초 진입은 유지한다. 보유 중에는 20개 연속 정규장 종가 평균 이상일 때 갱신하고, 청산 요청 뒤에는 잔고가 0이 될 때까지 매수를 막는다. 실제 전량 매도일로부터 5거래일 뒤부터 추세와 선정 조건을 다시 충족하면 재진입한다. 주문은 판단일 종가에 확정해 다음 정규장부터 처리한다.

2024년 4분기는 이미 본 개발 진단, 2025년 1분기는 학습 이후 다른 과거 평가다. 후자도 이미 가격을 열람한 자료여서 pristine OOS가 아니다. 세 거래팔×두 비용×5위상×두 기간=60계좌와 기간별 현금선2개를 비교한다. 각 위상의 초기자금1,000만원·슬롯1/6이고 후보 탈락 자리는 현금이다. 새 결과를 보고 기준·기간·비용·모델을 바꾸지 않는다.

과거20종가의 실제 단위 문제, 확인된 정지·필수가격 결손은 공통 중단으로 남긴다. 개장 체결 뒤 발견한 종가 결손은 이미 확인한 체결을 지우지 않는다. 권리/배당 전수 인증은 미완료라 가격 정책의 지지·기각과 역사 총수익/실전 승격을 구분한다. 총수익과 실전 승격은 이 미션에서 승인하지 않는다.

아래 JSON이 유일한 기계 계약이다. runner는 이 파일 바이트가 사전 커밋의 파일과 같음을 확인하고 해시를 결과에 남긴다. 기존 동결 계획·실행부·등록 조건은 수정하지 않는다. controller가 완전한 실행 결과를 새 로컬 연구 파일로 보존하고 결과 문서에 경로별 원장 요약·메트릭·중단 사유와 해시를 기록한다.

```json
{
  "schema": "sig14-chief-hold-renewal-contract-v1",
  "status": "chief_fixed_controller_precommit_required_before_results",
  "baseCommit": "c411069dcc0b8cfd89fd3713c59352deb0031e95",
  "baseCommitGitVerifiedThisReview": false,
  "failure": {
    "evidence": "SIG-8/9 보존 문서 및 지정 세 report revision",
    "r3MeanCostInitialCapitalPct": 5.806,
    "phase4NetCostSavingKRW": 135024,
    "phase4NetMinusForcedAfterCostPriceValueKRW": -27061,
    "provenance": "기존 기록 인용; 이번 재계산0"
  },
  "hypothesis": "추세가 유지되는 보유만 갱신하고 완전청산 뒤 5세션 대기하면 net의 불리한 잔존 노출을 줄이면서 forced보다 일부 비용을 절약해 비용 후 가격손익을 개선할 수 있다. 결합정책만 검증하며 두 요소의 독립 효과는 식별하지 않는다.",
  "designRationale": "MA20은 동결 R3의 distance_ma20에 이미 쓰는 한 달 추세, 5는 기존 판단/보유 지평이다. 효과 증거가 아니며 세 종목/날짜 맞춤·문턱튜닝·다른 후보 실행은 금지한다.",
  "inputs": {
    "model": {
      "path": "data/research/kr-ridge-r3-model-2026-10-17-v1.json",
      "expected_sha256": "81de9f736b6c3e853bfa7a8a30009551aa5757783e50adf8128e2726d737fdd0",
      "hash_provenance": "prior_contract_not_rehashed_this_run",
      "fit_trials": 0
    },
    "selectionPlan": {
      "path": "docs/kr-ridge-selection-plan-2026-10-17.json",
      "sha256": "5bf03e849b6503332b3eb9468e69be5b3d3691f21c6c50819f44c71e5d50417f"
    },
    "financial": {
      "binding": "kr_ridge_selection_lab.FINANCIAL 및 기존 load_financials; 같은 원본만 사용",
      "sha256": "bdab1956d2f1055a31a012fc8c3e5ecbb442de21c95c574beeb37aaf7c7d085c",
      "rosterAsOf": "2024-04-01",
      "availability": "metrics_asof의 당시 가용시각·기존 결측 마스크 유지; 최신 재무로 교체 금지"
    },
    "scope": {
      "binding": "kr_ridge_selection_lab.SCOPE",
      "sha256": "8b98fbf085b9b610ffe23afaf5ba84faa0da1d6de223ec42021c8eba923705f6"
    },
    "priceArchivesUnder": "data/research/",
    "priceArchiveSHA256": {
      "krx-daily-2023-q1-v1.zip": "e9bfadf172c80a91f1eca853037350db007f28d9b850f1b00d9774646ab1a8d0",
      "krx-daily-2023-q2-v1.zip": "a6f83ef305e8c5d7f277444af69af3030224633cd0c70da58096906909eb30e7",
      "krx-daily-2023-q3-v1.zip": "bc708b3ce35d2c28542f1b8c6423e3c771b5ea81b534e85c23dc506ae357788d",
      "krx-daily-2023-q4-v1.zip": "d0fdfdef45ccf488833f2b63a97d5503a4297f53a4918b3c5ab641adc852d5b1",
      "krx-daily-2024-q1-v1.zip": "f579892c94e216cde45eeaef9135d65ce9964fc878464cdf19af489dca473de3",
      "krx-daily-2024-q2-v1.zip": "ee7d94c0524165a07c4907e5ccf967632d2071e26329c29938b69f2f9173b4df",
      "krx-daily-2024-q3-v1.zip": "894466cf70e9ef64bcc7caedeeb3d24c498755f3d4cd13ecc86df9432e7cbf9e",
      "krx-daily-2024-10-12-v1.zip": "e48e8e8601ab7c9de23b5de6f9b331cff6784600b0e3694e8cf4d6a4b69afd02",
      "krx-daily-2025-01-04-v1.zip": "0f8ba69f3d8cecc3cd3a85e59925dc76920380cf1b565bfb96d6f1ab823cf419"
    },
    "priorEvidenceSHA256": {
      "data/research/kr-ridge-r3-result-2026-10-17-v4.json": "2cf98e96eff44d415c0264bd1ec1b6f2b036804b9e2bbeb28476dc807994abfd",
      "data/research/kr-ridge-r3-listing-originals-2026-10-17-v1.zip": "becd863bc6b9c9417922fd735736ea9dc95e2355c15da2c603f22606e5340f59",
      "data/research/kr-ridge-r3-halt-originals-2026-10-17-v2.zip": "32929ab5aa1c2738633d083ca6c2782da9e0d49882f2e911b1f4a969d496d2f0",
      "data/research/kr-ridge-turnover-diagnostic-2026-10-18-v1.json": "bdade2f189de9851ef53357fb2244914cd4e821d922cb19dd5a1956ed781e347"
    },
    "hashProvenance": "모델·자료 해시는 지정 보고에서 계승했으며 이번 미재해시. 아래 reviewedCodeSHA256만 본인이 바이트 재확인. controller가 결과 전 실제 파일과 대조한다.",
    "reviewedCodeSHA256": {
      "docs/KR-R3-TURNOVER-DIAGNOSTIC-2026-10-18.md": "34366fb6e81138520bad1047e8c6f4ef06f32d90a96be99f77bd85f541a817d1",
      "docs/kr-ridge-selection-plan-2026-10-17.json": "5bf03e849b6503332b3eb9468e69be5b3d3691f21c6c50819f44c71e5d50417f",
      "scripts/measure/kr_ridge_turnover_diagnostic.py": "baed786977f41b726bae6bd45406616b0fff4e315f905bc53e32d65504796a31",
      "tests/test_kr_ridge_turnover_diagnostic.py": "8f5a1ace41333ab718ad2ff816cb18e71fbb0070f966b7f6250151b38a411aba",
      "scripts/measure/kr_ridge_selection_lab.py": "4ab9e7e6d1c89c79ae861e59755d5180297317246c76e2f9b1740d9415eb3ba0",
      "src/signal_desk/signals/research_net_alpha_execution.py": "3d6c941185f7a8c35b77527dd49da240357751d1a2241ff4de48cf5957960625",
      "src/signal_desk/signals/research_net_alpha.py": "d0398de4c200ca2e59a1eb1e165c2805e94696314306e7d5532137fead67e70a",
      "src/signal_desk/signals/research_decision.py": "6ffc5df4277eda56453a06f306164ba619f3a6ee8cbec36b8287c3912567e8b8"
    },
    "calendarAndOverlap": "동결 market_clock의 kr 정규장 달력과 위 9개 ZIP만 사용. 중복 날짜는 정규화한 행이 동일할 때만 하나로 결합하고 provenance 모두 기록; 내용 충돌은 중단하며 최신 ZIP 우선 교체 금지. 달력·loader·predict·의존코드/명시 경로/전체 해시와 처리 규칙을 사전 manifest에 고정.",
    "noOtherArchives": true,
    "history": "각 t까지 253종가를 쓰는 원래 features_at 그대로. Q1 ZIP 날짜58개는 달력/가격 완전성 증거가 아니다. 원래 특성 제외/결측 마스크를 유지하고 전 top200 완전성을 새 문턱으로 추가하지 않는다."
  },
  "selection": {
    "model": "동결 R3(2024-04~09 학습), fit/label/run 호출0",
    "schedule": "기간 내 전체 정규장 D에 대해 D[phase::5] 종가 확정 후만 판단; 비판단일은 결제·기존 주문만 처리",
    "universe": "기존 당일 top200·재무 roster·특성·결측 마스크 그대로",
    "rank": "예측 내림차순, 동률 코드순; 상위6 내 predicted_gross>0.0125",
    "minFeatureNames": 160,
    "belowMin": "공통 S_t 공집합",
    "refill": false,
    "reproduceQ4": "기존 예측행·선정·순위와 대사; 불일치 시 중단. 옛 NAV/수익 차이를 정책 효과로 빼지 않음"
  },
  "policy": {
    "trend": "C_t >= arithmetic_mean(C of 20 consecutive regular sessions ending t); 등호 통과, 결손을 false/0으로 치환 금지",
    "stateScope": "각 기간·위상·비용 후보 계좌에서 독립 초기화; 보유 이력은 실제 양수 매수 체결부터",
    "neverHeld": "S_t이면 최초 신규진입 그대로. 추세 필터 추가 없음; 과거 매수 미체결은 미보유",
    "heldRenewal": "exit_pending=false이고 S_t 포함 AND trend이면 목표1/6으로 보유갱신·차액조정; 그 외 목표0 및 exit_pending=true",
    "exitPending": "실제 잔고0까지 목표0 유지·매수금지. 추세 회복에도 반전 금지. 참여율로 주문에 안 담긴 잔고도 보존. 정규 판단일마다 실제 잔고로 목표0 재제출, 잔량 교체 사유 기록",
    "flatTransition": "양수 잔고가 0이 된 최종 매도 체결일 f 저장; 의도일/결제일 사용 금지. 모든 실제 완전청산에 적용, 부분축소는 cooldown 시작 아님",
    "reentry": "정규 판단일 t에서 S_t AND trend AND session_index(t)-session_index(f)>=5. f+4 불허/f+5 허용; t+1 청산 후 t+5 판단은 보통 불허, 다음 위상 판단까지 대기",
    "forbidden": "t+1시가·미래종가·미래거래량으로 t 의도 변경 금지. 매수+1%한도는 추가/신규/재진입 매수에만; 갭을 본 뒤 같은 시가 신규 매도 금지"
  },
  "comparison": {
    "arms": {
      "forced": "매 판단일 기존 보유 매도+동결 S_t 재매수; force_roundtrip=True",
      "net": "동결 S_t 무조건 차액 주문; force_roundtrip=False",
      "candidate": "위 상태 정책으로 목표를 정한 차액 주문",
      "cash": "기간별 1,000만원 상수선, 이자 모델링 없음; 두 비용·위상 공유"
    },
    "initialCashKRW": 10000000,
    "constantTargetBaseKRW": 10000000,
    "slotWeight": "1/6",
    "budget": 1,
    "maxPositions": 6,
    "emptySlots": "현금; 재배분/차순위 충원 없음",
    "sizing": "core.orders에 공통 target base 사용, 팔별 NAV로 목표 확대 금지. 기존 보유·actual cash·시가 노출 제한 때문에 차액/체결수량은 팔마다 다를 수 있음. b=1은 계좌 노출한도, 개별 슬롯 시가한도 아님"
  },
  "execution": {
    "reuse": [
      "research_net_alpha.orders",
      "ExecutionBook.submit/open_session/price_nav",
      "기존 CommonDecision/observed_gap/reconcile_book의 적용 가능한 부분"
    ],
    "adapterOnly": [
      "세 팔·75/120bp spec",
      "상태 정책",
      "과거20종가 단위 연결",
      "FIFO 추가매수 귀속·공통중단·판정"
    ],
    "doNotCall": [
      "lab.run",
      "fit",
      "label_at",
      "옛 NAV/날짜 고정 replay_pair/diagnose/checked_inputs 전체",
      "두 팔 전용 submit_pair",
      "추가매수 불가 portfolio_stats"
    ],
    "decisionInputs": "t 확정 종가/거래량으로 주문량·한도·원천ID·우선순위 고정",
    "buyQuantity": "floor(부족액/(C_t*1.01*1.006))",
    "sellQuantity": "min(실제보유,ceil(초과액/C_t))",
    "volumeCap": "각 주문 min(quantity,floor(t거래량*0.01)); 미래 거래량 사용0",
    "priority": "원래 예측 순위/동률 코드순, 매도 우선",
    "firstFill": "다음 정규장 시가 대용; 실제 호가/체결 인증 아님",
    "buyLimit": "C_t*1.01",
    "buyRetry": false,
    "sellRetry": "최초 시도+최대5회; 소진 후 잔고/latch 보존, 다음 정규 판단만 새 제출. 마지막 terminal 뒤 매일 재생성 금지",
    "cash": "기존 T+2 signed settlement credit를 모의 매수여력에 포함; settled_cash/예정결제 분리. 미매도대금·미교부권리를 현금으로 만들지 않음",
    "caps": "시가의 팔별 현금/6자리/비용후 노출 제약은 매수량 감소·취소만 허용, 사전 수량 확대 금지",
    "roundtripBP": [
      75,
      120
    ],
    "oneWayBP": [
      37.5,
      60
    ],
    "fees": "체결원금에만 적용; 수량버퍼60bp와 구분, 갭손익 중복 차감 금지",
    "scenario": "full만 역사grid. 현금/한도/참여율 제약 유효; 합성 부분·지연·미체결은 경계검사 전용"
  },
  "periods": [
    {
      "id": "dev_2024q4",
      "decisionStart": "2024-10-02",
      "decisionEnd": "2024-12-30",
      "tailThrough": "2025-01-31",
      "expectedSessions": 61,
      "supportEvidence": false
    },
    {
      "id": "historical_price_2025q1",
      "decisionStart": "2025-01-02",
      "decisionEnd": "2025-03-31",
      "tailThrough": "2025-04-30",
      "zipDateMembers": 58,
      "calendarCoverageVerified": false,
      "supportEvidence": true,
      "pristineOOS": false
    }
  ],
  "terminal": {
    "L": "위상별 마지막 정규 선정일",
    "lastOrders": "L 이후 새 매수 의도 없음; L종가에 제출한 유효 매수는 L+1시가 최초 시도 허용, 잔량취소",
    "zeroTarget": "L+5번째 정규장 종가에서 모든 거래팔 목표0 제출, 이후 매수0",
    "tail": "기존 매도 재시도·결제만 관측, terminal 후 신규 잔량 재제출0",
    "incomplete": "꼬리 종료 보유·pending·T+2 settlement 중 하나라도 남으면 최종 성과/해당 쌍별차이 null; prefix 별도"
  },
  "grid": {
    "chiefFixed": true,
    "periods": 2,
    "phases": [
      0,
      1,
      2,
      3,
      4
    ],
    "roundtripBP": [
      75,
      120
    ],
    "tradingArms": [
      "forced",
      "net",
      "candidate"
    ],
    "tradingPaths": 60,
    "sharedCashSeries": 2,
    "totalPathsMax": 62,
    "order": "period(dev,Q1),cost(75,120),phase(0..4),세 팔 동기 replay",
    "independentSamples": false,
    "expand": false
  },
  "metrics": {
    "trace": "S_t·추세 입력/판정·holding/exit_pending/ever_held/f/cooldown·목표·제출/체결/취소/잔량·원천ID",
    "ledger": "매수/매도원금·체결비용·회전율=(매수+매도원금)/초기자금·settled cash/settlement/buying_power·노출금액/비율·보유세션·갭/한도/현금/자리/노출 축소 사유",
    "value": "V_t=buying_power+sum(q*C_t); settlement 다시 더하지 않음. afterCostPricePnL=V_t-initial; beforeCostPricePnL=afterCostPricePnL+fees",
    "gap": "day pricePnL before cost=sum(q_prev*(O_t-C_prev))+sum(q_after_open*(C_t-O_t)); 단위 일관성·원장 대사 필수",
    "deltas": "candidate-forced와 candidate-net 각각 ΔV=ΔbeforeCostPricePnL−Δfees; 비용으로 바뀐 수량/노출 효과 포함, 순수 알파·독립 인과 주장 금지",
    "fifo": "매수 체결 한 건=lot1; 매수비용 수량비례 배부, 매도 FIFO에 원금·매도비용 수량비례 배부; lot전량 매도 때만 완결1. 진입월=매수체결월. 잔여lot 기말가격평가 포함",
    "risk": "비용후 V곡선 가격MDD; 종목집중도=max(abs(종목가격PnL))/sum(abs(종목가격PnL)); 분모0은 null",
    "tolerance": "원장·FIFO·갭·비용·Δ 대사 절대오차<=0.01원; 정수수량 보존 및 buying_power>=−1e−6원"
  },
  "supportGatesQ1": {
    "scope": "두 비용·5위상 모두 개별 판정, 평균/4-of-5 대체 금지; Q4는 문턱에 사용0",
    "completeness": "모든 세 팔×두 비용×5위상 완결",
    "minimumEachArmCostPhase": {
      "completedFIFOBuyLots": 12,
      "distinctBuyFillDates": 6,
      "distinctTickers": 6
    },
    "75bpEachPhase": [
      "candidate afterCostPricePnL>0",
      "candidate−forced afterCostPricePnL>0",
      "candidate−net afterCostPricePnL>0",
      "candidate fees<forced fees"
    ],
    "120bpEachPhase": [
      "candidate afterCostPricePnL>=0",
      "candidate−forced afterCostPricePnL>=0",
      "candidate−net afterCostPricePnL>=0"
    ],
    "candidateEachCostPhase": {
      "priceMDDPctMinimum": -10,
      "absoluteTickerPnLShareMaximum": 0.5,
      "positiveEntryMonthsMinimum": 2
    },
    "noRequirement": "후보 비용이 net보다 낮아야 한다는 추가 조건은 두지 않는다.",
    "outcomes": {
      "allPass": "개발 연구용 부분가격 정책 지지/개발채택만; 현재 adoptionClaim=false, 실행후 별도 판정",
      "observedGateFailure": "해당 개선 주장 기각; 다른 결손이 있어도 관측 실패 보존",
      "minimumNotMet": "지지 정보 부족·개발채택 불가; 기간연장/문턱완화0",
      "sourceOrTimeoutIncomplete": "전체 지지 판정 불가·채택 불가; 결손을0/삭제하지 않음",
      "implementationFailure": "경제적 기각과 구분해 실행무효·종료; 같은 입력 재실행0"
    },
    "historicalResearchAdoption": false,
    "livePromotion": false,
    "certifiedTotalReturn": null,
    "certifiedTotalReturnMDD": null
  },
  "jointSourceStop": {
    "scope": "같은 기간·위상·비용의 forced/net/candidate 동시",
    "names": "세 팔 보유·pending·공통 S_t와 실제 사용하는 추세/특성 이력의 합집합; 원래 제외/결측마스크 유지, 미래 생존마스크 금지",
    "triggers": [
      "실제 단위변동/비교단위 불일치 또는 검수 안 된 주식수·권리변동",
      "확인된 정지",
      "필수 시가/종가/거래량/사용 MA20·특성 이력 결손"
    ],
    "historyRule": "보유하지 않았던 종목도 실제 쓰는 과거20종가와 알려진 특성 단위문제를 점검. observed_gap의 당일/전일 검사만으로 MA20 적격 인증 금지",
    "timing": "개장에 알 수 있는 결손은 체결전 중단; 종가에서만 확인되면 이미 확정한 시가체결 보존하고 해당 종가 이후 평가/주문 null. 하루말 LIST_SHRS를 개장 관측으로 소급하지 않음; 시점 불명 단위문제는 비교불능 중단",
    "after": "마지막 공통 비교시각·날짜/종목/필드/원천/사유 보존, 이후세팔 성과/쌍별차이 null. cash선으로 합격대체 금지; 삭제/보간/0대체0",
    "genericDividendGap": "배당/행사 전수 미인증은 부분가격 한계와 역사채택 금지로 표시; 유효 prefix 비용/노출 분석 자체의 일괄금지 사유 아님",
    "resumeEvidence": "정확한 누락 원본 바이트 또는 해당 단위/권리 정산의 새 증거. 이번 수집0; 같은 해시/성공응답/최신본으로 반복 재개0"
  },
  "counterarguments": {
    "resolved": [
      "P1 익일시가 관측 후 동일시가 역매도 금지",
      "P1 과거 MA20 단위점검을 공통중단에 연결",
      "P1 L주문 익일 최초시도 허용으로 종료문구 수정",
      "P1 부분가격 합격의 총수익/역사채택 승격 금지",
      "P2 공통목표예산과 서로 다른 실제체결 구별",
      "P2 세팔·두비용·FIFO는 새 어댑터에 연결, frozen파일 변경0"
    ],
    "economicCounterexamples": [
      "MA20 위에서도 익일 급락 가능; forced는 갭 매수취소로 현금에 남고 후보는 보유 가능",
      "MA20 아래 최초 진입 뒤 다음 판단까지 손실 가능",
      "sticky청산·cool5로 급반등/재진입을 놓치고 net보다 비용 증가 가능"
    ],
    "responseStatus": "세 지정 보고에 QA 이후 정량 회신은 없다. 회신을 수행했다고 주장하지 않는다. 추가 호출 금지인 이번 배정에 따라 chief가 모든 명세 반박을 직접 반영해 결정하며 경제적 반례는 기각 가능성으로 보존한다."
  },
  "implementationHandoff": {
    "singleTask": "새 연구 어댑터1개에서 위 상태기계·공통선정/고정예산·세장부·공통중단·FIFO·가격문턱 연결",
    "engineeringWrites": "staging의 지정 연구코드1·tests1·새 docs 계약/결과2만; frozen서비스/실행부/기존결과/장부 변경0",
    "contractJSON": "계약 Markdown 안 JSON객체 1개 포함; runner가 고정 바이트/JSON해시를 검증. 독립 JSON파일 추가 없음",
    "resultDestination": "새 docs 결과 문서에 경로별 원장/메트릭/null사유의 기계판독 JSON을 포함; 기존 data/research 결과 덮어쓰기0",
    "boundaryTests": [
      "t이후 시가/종가/거래량 변형에도 S·trend·수량·한도 불변; MA20등호/최초미체결/f+4·f+5/부분청산/sticky/L주문 익일체결",
      "추가매수 FIFO/원금·비용·갭·T+2·잔존자리 대사; 과거 추세창단위 결손·세팔 동시null·종가결손 전 시가체결 보존·불완전꼬리null"
    ],
    "controllerSequence": [
      "이 계약 문서+embedded JSON 및 전체 고정 입력/코드 manifest를 결과전 커밋",
      "staging검수·구현자와 별도 QA 코드리뷰·경계검사",
      "실제 역사 replay 단1회·원장대사·위 사전문턱 판정",
      "현재 HEAD CI 성공과 리뷰 확인 후 기존 승인범위 PR/merge"
    ],
    "noNewApprovalQuestion": true,
    "chiefPerformed": "계약확정·자기report저장만; 구현/검사/진단/사전커밋/CI/merge 완료를 주장하지 않음"
  },
  "limits": {
    "controllerActualRuns": 1,
    "cpuSeconds": 600,
    "wallSeconds": 600,
    "candidates": 1,
    "collection": 0,
    "fit": 0,
    "parameterSearch": 0,
    "sameInputReruns": 0,
    "timeout": "완료 경로와 prefix 보존, 미완료/미실행 성과 null; 예산 늘리기·다른 기간 대체0"
  },
  "locks": "기존 R3 불채택·R5/210경로 자료 차단·등록 문턱/PIT/기간/가중치·운영 주문은 그대로. 전문 금융스킬/별도 전문가 모델 확보 주장0.",
  "currentReview": {
    "serviceEdits": 0,
    "gitCommands": 0,
    "testsRun": 0,
    "diagnosticRuns": 0,
    "newReturnCalculations": 0,
    "sourceAcquisitions": 0,
    "delegations": 0,
    "scopeCompletion": "SIG-14 단일 계약 결정 보고만 완료; 상위 SIG-10의 구현·백테스트·채택·CI·merge는 미완료"
  },
  "candidate_id": "kr-r3-h5-ma20-renew-cool5-v1",
  "chiefReportRevision": "4f6bee39-6828-4679-97a7-50c613272b75",
  "controllerVerifiedInputSHA256": {
    "data/research/krx-daily-2023-q1-v1.zip": "e9bfadf172c80a91f1eca853037350db007f28d9b850f1b00d9774646ab1a8d0",
    "data/research/krx-daily-2023-q2-v1.zip": "a6f83ef305e8c5d7f277444af69af3030224633cd0c70da58096906909eb30e7",
    "data/research/krx-daily-2023-q3-v1.zip": "bc708b3ce35d2c28542f1b8c6423e3c771b5ea81b534e85c23dc506ae357788d",
    "data/research/krx-daily-2023-q4-v1.zip": "d0fdfdef45ccf488833f2b63a97d5503a4297f53a4918b3c5ab641adc852d5b1",
    "data/research/krx-daily-2024-q1-v1.zip": "f579892c94e216cde45eeaef9135d65ce9964fc878464cdf19af489dca473de3",
    "data/research/krx-daily-2024-q2-v1.zip": "ee7d94c0524165a07c4907e5ccf967632d2071e26329c29938b69f2f9173b4df",
    "data/research/krx-daily-2024-q3-v1.zip": "894466cf70e9ef64bcc7caedeeb3d24c498755f3d4cd13ecc86df9432e7cbf9e",
    "data/research/krx-daily-2024-10-12-v1.zip": "e48e8e8601ab7c9de23b5de6f9b331cff6784600b0e3694e8cf4d6a4b69afd02",
    "data/research/krx-daily-2025-01-04-v1.zip": "0f8ba69f3d8cecc3cd3a85e59925dc76920380cf1b565bfb96d6f1ab823cf419",
    "scripts/measure/kr_ridge_selection_lab.py": "4ab9e7e6d1c89c79ae861e59755d5180297317246c76e2f9b1740d9415eb3ba0",
    "src/signal_desk/signals/research_ridge.py": "de82fd6c9fc344929fbab74c7ea7bba63eb659b2474ebb5c213f59b7fe2f0ee4",
    "scripts/measure/kr_ridge_action_sources.py": "6f1e06f6efbc6bc68c456b33964d8b4f1ea069d5412a28deaa333a431c2f8cb7",
    "src/signal_desk/signals/research_action_coverage.py": "068006cc9e6b812f016915c12a2e61b1af3ed6e4bbad095e6590263c003a2401",
    "src/signal_desk/signals/research_daily_simulator.py": "10b92372f318ef0d223cfdfed71e1e0d8419736cace81e180c494dd38024a9f3",
    "src/signal_desk/signals/research_decision.py": "6ffc5df4277eda56453a06f306164ba619f3a6ee8cbec36b8287c3912567e8b8",
    "src/signal_desk/signals/research_financial_inputs.py": "d9a3e20d0413e807e9a67c5204a04abdafd4fa5812215df14f05e9a2f9276148",
    "src/signal_desk/signals/engine.py": "1d5ecfd102d5dc89c3635e91da0a45bc3ac772d89612cbc5deaf6288fa569447",
    "data/research/kr-ridge-r3-model-2026-10-17-v1.json": "81de9f736b6c3e853bfa7a8a30009551aa5757783e50adf8128e2726d737fdd0",
    "data/research/kr-ridge-r3-result-2026-10-17-v4.json": "2cf98e96eff44d415c0264bd1ec1b6f2b036804b9e2bbeb28476dc807994abfd",
    "data/research/kr-receipt-financial-inputs-2026-10-17-v1.zip": "bdab1956d2f1055a31a012fc8c3e5ecbb442de21c95c574beeb37aaf7c7d085c",
    "data/research/kr-selection-holding-audit-2026-10-17-v3.json": "8b98fbf085b9b610ffe23afaf5ba84faa0da1d6de223ec42021c8eba923705f6",
    "data/research/kr-ridge-r3-listing-originals-2026-10-17-v1.zip": "becd863bc6b9c9417922fd735736ea9dc95e2355c15da2c603f22606e5340f59",
    "scripts/measure/kr_ridge_turnover_diagnostic.py": "baed786977f41b726bae6bd45406616b0fff4e315f905bc53e32d65504796a31",
    "src/signal_desk/signals/research_net_alpha.py": "d0398de4c200ca2e59a1eb1e165c2805e94696314306e7d5532137fead67e70a",
    "src/signal_desk/signals/research_net_alpha_execution.py": "3d6c941185f7a8c35b77527dd49da240357751d1a2241ff4de48cf5957960625",
    "src/signal_desk/market_clock.py": "0ad001d935b0252529e1b882de8afc1eaa2482220ded44580aad57136704f727",
    "scripts/measure/krx_price_replay_pilot.py": "3f0e62787a94f3c9f0b2c8c8cb2743106e2a80e44d3dded4b7449ca626b735d7",
    "scripts/measure/price_challenger_lab.py": "c8a7c7166e819a1f9a6cad4c19d89429f7d81d738b812c8dcd7c548b8bd2827d",
    "scripts/measure/kr_original_financial_sources.py": "adbe0208ac952e1864cf463af275b7ad96584760b049040488d3a61d29ed1a71",
    "scripts/measure/kr_financial_input_sources.py": "7af3bde934a017303bf625d2875566c615054db618f55b83dd292980ce00424e",
    "docs/KR-R3-TURNOVER-DIAGNOSTIC-2026-10-18.md": "34366fb6e81138520bad1047e8c6f4ef06f32d90a96be99f77bd85f541a817d1",
    "docs/kr-ridge-selection-plan-2026-10-17.json": "5bf03e849b6503332b3eb9468e69be5b3d3691f21c6c50819f44c71e5d50417f",
    "tests/test_kr_ridge_turnover_diagnostic.py": "8f5a1ace41333ab718ad2ff816cb18e71fbb0070f966b7f6250151b38a411aba",
    "data/research/kr-ridge-r3-halt-originals-2026-10-17-v2.zip": "32929ab5aa1c2738633d083ca6c2782da9e0d49882f2e911b1f4a969d496d2f0",
    "data/research/kr-ridge-turnover-diagnostic-2026-10-18-v1.json": "bdade2f189de9851ef53357fb2244914cd4e821d922cb19dd5a1956ed781e347"
  },
  "controllerNote": "chief의 판단 규칙·기간·비용·문턱은 유지하고 실제 파일 해시를 추가했다. 원천/모델을 재학습하거나 수익을 계산하지 않았다."
}
```
