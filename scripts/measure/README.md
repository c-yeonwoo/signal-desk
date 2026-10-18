# 측정 스크립트 (2026-09-06~07 세션)

하네스 노브로 흡수되지 **않은** 일회성 측정들. 결과는 모두
[`docs/harness-findings.md`](../../docs/harness-findings.md)에 기록돼 있고,
여기 있는 것은 **재현용**이다.

```bash
.venv/bin/python scripts/measure/<script>.py       # 리포 루트에서
```

## 2026-10-18 비용·시장·선정 분리 후보

[고정 조건](../../docs/KR-NET-ALPHA-PLAN-2026-10-17.md)과 [실제 원천/중단 보고서](../../docs/KR-NET-ALPHA-READINESS-2026-10-18.md)를 먼저 확인한다. `kr_net_alpha_sources prices`는2020~2022 KRX 원시 응답을 재개 가능한 로컬 장부로 보존하고, `identities`는 기존2023/2024년1월 원문과 과거 공식 주식종류·ISIN을 연결한다. 새 가격/기본정보 요청은 총1,200회 한도이며 실패·중단도 센다. 계획 커밋 `8c6d837a91f856ade03b75bdaec672abd51e051d`와 문서/JSON 해시가 다르면 실행하지 않는다. 얕은 복제본은 해당 객체를 먼저 fetch한다.

`.venv/bin/python -m scripts.measure.kr_net_alpha_readiness --output <새 JSON>`은 수익률 없이 전체 날짜·종목의 원시 가격쌍·행사 범위·주식수 변경 대기열을 재현한다. 검증 라벨 묶음이 없고 v1 U의2021년 원시 시가쌍이91일이라 `blocked_data`다. 당일 무거래 제외 가정의188일은 자료 진단만이며 실제 학습에 적용하지 않았다. `research_net_alpha.py`는 회귀·보정·비용 목적함수의 핵심 수치 구현이다. 실제 모델 학습/210경로 체결 백테스트 실행기로 부르지 않는다.

[v2 보완 계약](../../docs/KR-NET-ALPHA-TRADABILITY-2026-10-18.md)에서 대상·결손 라벨·보유 정지의 설계 판단을 완료했다. 다음 Sol 작업은 부모/보완 계약 로더, 당시 거래 가능 U와 원천/라벨 빌더, 실제180/80일 검사, 동결 학습 및210경로 구현이다. 현재 위 명령은 **v1 진단**이며 v2 준비 완료를 검사하지 않는다. 기존 원문·진단은 보존하고, 보완 계약을 구현하지 않은 함수에 v2 ID만 넣어 학습하지 않는다.

**v2 구현/실측:** [보고서](../../docs/KR-NET-ALPHA-V2-IMPLEMENTATION-2026-10-18.md). `.venv/bin/python -m scripts.measure.kr_net_alpha_v2 --evidence <검수 묶음 JSON> --output <새 .json.gz> --fit-output <첫 모델 JSON>`은 원문·범위 리뷰·당시 U·고정h5 라벨을 확인해 실제 준비 날짜를 센다.180/80일이 충족될 때만 학습/보정하며 기존 모델 경로는 재학습하지 않는다. 현재 아시아나 공개시각 검수 묶음에는 전체 인증서가 없어 학습은 열리지 않는다. 전체 감사는 gzip으로 보존할 수 있다. 아직210경로의 상태형 실행기·채점은 구현하지 않았다.

`kr_net_alpha_halt_publication --output <새 ZIP> --packet <새 JSON>`은 고정 아시아나 원문과 KIND 목록 시각만 대조한다. 문서번호를 접수번호로 쓰지 않는다. `kr_net_alpha_bulk_schedule --output <새 ZIP>`은 공식 공백 종목코드의2021년3월 배당 일정 양성 대조를 전체 종목으로 조회한다. 기존 로컬 KIS 환경을 유지하며 기본 최대5페이지·월 범위·누적400시도·1초 간격을 적용한다. 페이지 종료를 배당락·효력일 범위 인증으로 바꾸지 않는다.

**효력일·정정 원천 검수:** [3,435건 목록·31건 원문 대조](../../docs/KR-NET-ALPHA-EFFECTIVE-SOURCE-AUDIT-2026-10-18.md). 아래 수집기는 연구 폴더의 누적 장부를 공유한다. 실패 응답도 보존하고, 회사 없는 시장 공통 공지·우선주 제목·뷰어의 정정 버전을 남긴다. 정확한 문서 버전이 모호하면 최신 원문을 임의 선택하지 않는다. `snapshot`은 실제 보존 응답·관측을 묶을 뿐 행사 범위 인증이나 학습 자료를 생성하지 않는다.

```bash
# 같은 요청은 바이트 해시를 검증한 캐시에서 재개한다.
.venv/bin/python -m scripts.measure.kr_action_source_audit \
  --start 2021-01-01 --end 2021-12-31 --max-pages 20 \
  --output data/research/<새 목록 이름>.json

# 같은 로컬 원문 조사 상태를 새 묶음으로 보존한다. 기존 묶음은 덮어쓰지 않는다.
.venv/bin/python -m scripts.measure.kr_action_source_audit --snapshot \
  --output data/research/<새 원천 묶음 이름>.zip
```

카카오 결정문 효력일04-14와 거래소 가격 적용일04-15, 현대중공업지주 최초/정정 일정, 쌍용차 호가단위 정정은 서로 다른 역할이다. 현재 완전 날짜는 학습0/180·보정0/80이며210경로 미실행이다. KIS 원시 일봉의 락 코드가 실제 분할에서도00인 반례가 있어 `행사 없음` 인증으로 사용하지 않는다.

**날짜 명세 구현·범위 감사:** [실측 보고서](../../docs/KR-NET-ALPHA-ACTION-CLOCK-IMPLEMENTATION-2026-10-18.md). `kr_action_clock_audit --packet <새 JSON> --readiness <새 JSON.gz> --report <새 JSON.gz> --fit-output <첫 모델 JSON>`은 고정 원문과 공식 과거 증권 식별로 구성요소를 검수하고, 실제 잠정U의 입력/결과 의존 구간을 합쳐 인증서를 검사한다. 새 증거 스키마는 `net-alpha-action-components-v1`이며 `assertions`·`identity_bindings`를 원문에 묶는다. `effective_at`만 있는 경제 사건은 거부하고 기존 정지 입력은 호환한다. 전체 범위 인증 없이 선별 양성 사례로 학습을 열지 않는다. 현재1,688범위는 미인증이며 신규 모델과210경로 성과는 생성하지 않았다. 코드/원천이 달라지면 기존 감사 출력은 덮어쓰지 않고 새 이름을 사용한다.

**금융위 과거 권리일정 후속:** [인증·실제 대조 보고서](../../docs/KR-NET-ALPHA-FSC-RIGHTS-2026-10-18.md). `kr_net_alpha_fsc_rights`의 탐침·`--crosscheck`·`--inspect`로 원문을 보존하고 정확한 회사와 날짜 역할을 대조한다. 로컬 `DATA_GO_KR_SERVICE_KEY`로 과거 조회는 성공했으나 부분 일치 회사 검색, 액면가별 중복, 기준일/권리일/교부일을 구분해야 한다. 완전 페이지 수신은 효력 기간·정정 전수 인증이 아니며, 새 학습·210경로·운영 승격은 하지 않았다.

**발행·변경상장 후속:** [고정6종목 원문9개 대조](../../docs/KR-NET-ALPHA-LISTING-CROSSCHECK-2026-10-18.md). `kr_net_alpha_listing_crosscheck --output <새 ZIP> --report <새 JSON>`은 KIND 원문 ISIN/주식종류와 단순 분할의 실제 수량을 대조한다. 발행정보 서비스 권한 반영 뒤에만 `--recheck-issuance`로 한 번 새 요청하며, 과거 거부 응답은 보존한다. 복합 분할·미연결 증권을 단순 계수나 행사 없음으로 바꾸지 않고 전체 범위 인증·학습·210경로를 자동으로 열지 않는다.

**새 후보 순수량 실행부:** [구현과 남은 경계](../../docs/KR-NET-ALPHA-EXECUTION-2026-10-18.md). `research_net_alpha_execution`의 `portfolio_plan`·`ExecutionBook`은 기존 고정6정책의 목표와 현금/잔량/부분체결을 처리한다. `path_grid`의210개 키 생성이나 합성 비용 대사는 실제210경로 성과 실행을 뜻하지 않는다. 원천 인증·기업행사/권리 연동·최종 채점 없이 가격 진단을 총수익으로 표시하지 않는다.

**권리·달력 계좌 재생:** [연결 범위와 실제 차단 결과](../../docs/KR-NET-ALPHA-ACCOUNT-REPLAY-2026-10-18.md). 다음은 기존 v10의 준비 관문을 원문/구현 해시와 행별 표본으로 재검증한다. 현재 학습0/180·보정0/80이라 실제 경로는0개이며 수익 비교를 생성하지 않는다. 새 원천/코드에는 새 준비 보고서·출력 이름을 사용한다. `--model`은 자료 충족 후 기존 준비 보고서에 해시가 묶인 1회 학습 산출물만 허용한다. 자료 충족 시 원천을 전부 다시 검수한 뒤 210계좌를 반복하지만, 별도 통계·귀속·채택 관문은 아직 남는다.

```bash
.venv/bin/python -m scripts.measure.kr_net_alpha_replay \
  --readiness data/research/kr-net-alpha-v2-readiness-2026-10-18-v10.json.gz \
  --readiness-sha256 8812b6952941451e01c931399da30ed80f3b16a25286c7740f3ffcd1b847f87f \
  --evidence data/research/kr-net-alpha-action-clock-packet-2026-10-18-v2.json \
  --output data/research/kr-net-alpha-account-replay-2026-10-18-v5.json.gz
```

**예탁결제원 공개 과거 원천:** [실제232행·기간 분할 불일치·인증 한계](../../docs/KR-NET-ALPHA-SEIBRO-SCOPE-2026-10-18.md). `kr_net_alpha_seibro_scope --output <새 ZIP> --report <새 JSON>`은 API키 없이 공개된 과거 일정·배당/분할 상세만 읽고 원문을 보존한다. 시장 전체 최대7일·발행사 최대365일 한도를 지킨다. 정확한 보통주 선택, 수량/날짜 역할, 연간/반기 합집합을 비교하지만 전수·정정 인증서를 자동 생성하지 않는다. 현재225→255/400행사 요청, 새 인증0·학습/실제210경로 미실행이다.

## 2026-10-17 단일 Ridge 후보 학습·평가

[조건](../../docs/KR-RIDGE-SELECTION-PLAN-2026-10-17.md)을 첫 학습 전 커밋하고 [결과](../../docs/KR-RIDGE-SELECTION-RESULT-2026-10-17.md)를 분리했다. 학습116일·21,884행의 모델 하나를 고정해2024년4분기61일·5위상·6실행조건·5대조를 재생한다. 주 비용 평균은 양수지만 손실 위상·낙폭·비용/체결 스트레스 때문에 **미채택**이며, GS리테일 분할 거래정지의 대조2경로가 남아 전체 형식 판정은 보류다. 결과 보고 문턱이나 학습 기간을 바꾸지 않는다.

`.venv/bin/python -m scripts.measure.kr_ridge_selection_lab --model data/research/kr-ridge-r3-model-2026-10-17-v1.json --listing-archive data/research/kr-ridge-r3-listing-originals-2026-10-17-v1.zip --output data/research/<새 이름>.json`으로 보존 자료를 오프라인 재생한다. 기존 모델 파일은 학습 입력 해시를 대조해 재사용하며 재학습하지 않는다. 처음 실행 시에만 학습 모델을 새 파일로 저장하고 평가를 시작한다. 기존 원문·결과 파일을 덮어쓰지 않는다. 얕은 복제본은 계획 커밋 `c2a0b3ee14ee8e3bca63782fbc41ec4611a61113`을 먼저 fetch해야 한다.

원문 획득은 `kr_ridge_action_sources --result <기존 결손 결과> --output <새 ZIP>`으로 분리한다. KOSPI·해당 날짜 전14일·최대100요청의 공식 KIND 검색/뷰어/원문을 보존한다. 재조회 ZIP은 관측시각과 버전이 달라질 수 있어 고정 해시의 연구 입력을 자동 교체하지 않는다. 소각·옵션/CB 발행의 선별 사실은 보통주 보유수량 불변만 확인하며 기업행사 전수 인증이 아니다. 연구 코드는 운영 계산·주문·보호 구간을 건드리지 않는다.

## 2026-10-17 종료한 당일 매매 비교

`.venv/bin/python -m scripts.measure.research_daytrade_batch --output data/research/<새 이름>.json`은 공식 KRX ZIP 13개의 해시·날짜를 검사하고 개발 7창·별도 2025년 4블록을 같은 초기자본으로 계산한다. 전일 자료의 세 후보와 현행 가격 전용·대형주·현금 대조군, 45/75/120bp·전량/절반/미체결을 비교한다. 주문 수량은 다음 날 시가를 보기 전에 고정하며 당일 청산 관측이 없으면 해당 경로의 수익을 보류한다. 결과와 원문은 로컬 연구 자료다. [고정 규칙과 첫 결과](../../docs/DAYTRADE-BENCHMARK-2026-10-17.md)에서 세 후보는 모두 **기각·종료**했다. 같은 확인 구간을 재튜닝의 최종 시험으로 재사용하지 않는다. 운영 엔진과 등록 연구를 실행하거나 변경하지 않는다.

## 2026-10-17 종료한 계약 공시 반응 전략

공식 KIND 공개 시각→완료된 5분 가격·거래량→지연 진입→60분 뒤 관측을 한 정책으로 재생한다. 1,268개 공시에서 **가격 조회 전 45개 사건**을 동결했고 5월 개발·6/7월 확인으로 나눴다. [조건과 결과](../../docs/CONTRACT-EVENT-BACKTEST-2026-10-17.md) 및 [보존 요약](../../docs/contract-event-backtest-2026-10-17.json)에서 **기각·종료**했다. 확인 두 월의 평균 계좌 순수익 −0.3135%(왕복 75bp), 시장 대조 +0.0813%이며 일부 즉시 진입 대조는 청산 분봉 결손으로 null이다. 이 전략은 운영 점수·주문에 연결되지 않는다.

원문 수집은 `python -m scripts.measure.contract_event_sources filings --output <새 ZIP>`, 분봉은 `minutes --event-archive <동결 공시 ZIP> --output <새 ZIP>`으로 분리한다. `--reuse-source <같은 종류의 기존 ZIP>`은 검증된 원문 바이트/관측 시각을 유지하며 실패한 조회만 새 파일에 재시도한다. 과거 공시는 공개 인덱스를 다시 조회하므로 새 모집단을 기존 확인 결과와 섞지 않는다. 분봉은 기존 KIS 키로 프로세스 내 실환경 **GET 조회만** 하며 `.env`·운영·계좌 주문을 변경하지 않는다. 키를 출력하지 않는다.

보존 ZIP과 KOSPI 식별 대조용 기존 두 KRX ZIP이 있으면 `.venv/bin/python -m scripts.measure.contract_event_backtest --event-archive data/research/contract-event-filings-2026-10-17-v4.zip --minute-archive data/research/contract-event-minutes-2026-10-17-v2.zip --output data/research/<새 이름>.json`으로 네트워크 없이 재현한다. 해시·공개 시각·회사 귀속·금액·날짜를 먼저 대조한다. 가격 구멍을 보간하지 않고 불완전 계좌 수익을 null로 유지한다. 당시 공급자 버전/호가/실체결·KOSDAQ 독립 과거 계보는 인증하지 않았으므로 등록 판정이나 실전 적격 증거가 아니다.

## 2026-10-17 종료한 실적·가이던스 연구

두 시장은 모집단과 실행 시각을 분리했다. [국내 회사 전망 대비 상향](../../docs/EARNINGS-GUIDANCE-BACKTEST-2026-10-17.md)은 2025년 선행 전망까지 원문으로 대조했지만 적격 개발 1건/확인 0건이라 **표본 부족으로 채택 거부**다. [미국 EPS 예상 대비 실적](../../docs/US-EARNINGS-SURPRISE-BACKTEST-2026-10-17.md)은 8개 고정 회사 64개 발표에서 2025년 16거래·75bp 계좌 수익 **−0.9603%**로 **성능 실패 기각**이다. 구현과 백테스트는 완료했으며, 추가 관측 대기 전략이나 운영 매수 필터로 남기지 않는다.

국내 원문 수집은 `python -m scripts.measure.earnings_event_sources --output <새 ZIP>`, `--reuse-source <기존 ZIP>`으로 재사용한다. 2025년 전망은 2026년 회사의 선행 기준으로만 쓴다. `earnings_event_backtest freeze --source-archive <원문 ZIP> --output <새 사건 ZIP>`에서 비교 가능한 기간·단위·회사 기준으로 표본을 고정한 뒤 `minutes --event-archive <사건 ZIP> --output <새 분봉 ZIP>`으로 읽기 전용 KIS 조회를 한다. `run --source-archive ... --event-archive ... --minute-archive ... --output <새 JSON>`은 원문에서 표본을 재계산해 같은지 확인한 후 오프라인 재생한다. [보존 요약](../../docs/earnings-guidance-backtest-2026-10-17.json)에 해시와 결과가 있다.

미국은 `python -m scripts.measure.us_earnings_backtest eps --output <새 EPS ZIP>` → `prices --eps-archive <EPS ZIP> --output <새 일봉 ZIP>` → `run --eps-archive <EPS ZIP> --price-archive <일봉 ZIP> --output <새 JSON>` 순서다. EPS 원천과 표본을 먼저 동결하고 `MODP=0`·과거 기준일만 조회한다. 키는 기존 환경에서 읽고 기록하지 않는다. [보존 요약](../../docs/us-eps-backtest-2026-10-17.json)과 문서의 파일명으로 재현하며 재조회는 새 관측이다. 예상치의 당시 공급 버전·정확한 최초 공개 시각·실체결을 인증한 것이 아니므로 등록 판정·실주문은 항상 false다. 현재 분기 예상치나 실제 시가로 과거 EPS/주문 수량을 채우지 않는다.

## 2026-10-17 실적 기사 시각 → SIP 호가 진입·청산

[고정 계획](../../docs/EARNINGS-ANNOUNCEMENT-EXECUTION-2026-10-17.md)은 8회사·2022 개발/2023 확인의 64발표를 유지한다. 기사 `created_at`과 `updated_at` 중 늦은 시각+처리30초, 당시 ask로 정한 주문 한도와 최소1초 뒤 체결 모델, 실제 진입+60분 뒤 bid 청산을 연결한다. 현재 vendor 예상치를 과거에 붙이거나 분봉 종가로 호가를 대신하지 않는다. 최초 공개/과거 실정산 인증과 **피드 한정 조건부 재구성**을 분리한다.

`earnings_announcement_execution cohort` → `inputs` → `reference`/`entry`/`exit`/`gaps` → `report` 순서다. 수집 요청은 운영 관리자 전용 Alpaca GET 경로로 수행하며 키를 로컬로 옮기지 않는다. 원문 JSON·해시·빈 구간·부분 페이지와 실패 시도는 로컬 `data/research/`에 남긴다. `gaps`는 완전히 관측된 구간만 이어 보고 부분 페이지는 좁혀 확인한다. 조회 상한 때문에 못 찾은 호가는 미체결 0%가 아니라 결손이다.

```bash
# 기존 보존 자료만 재생한다. 새 출력명만 허용한다. 네트워크·주문·운영 DB 접근 없음.
.venv/bin/python -m scripts.measure.earnings_announcement_execution report \
  --input data/research/earnings-announcement-inputs-2026-10-17-v3.json \
  --sources data/research/alpaca-reference-2026-10-17-[0-9]*.json \
    data/research/alpaca-entry-2026-10-17-[0-9]*.json \
    data/research/alpaca-exit-2026-10-17-[0-9]*.json \
    data/research/alpaca-gaps-2026-10-17-r*-b*.json \
  --output data/research/earnings-announcement-replay-NEW.json
```

`earnings_announcement_delivery --output <새 JSON> --archive <새 ZIP> --public <새 요약 JSON>`은 동일 계산에 원문 묶음과 원문 대조·해시·응답량을 더한다. 기존 ZIP은 모든 보존 바이트가 일치할 때만 검증 후 재사용한다. 공개 요약에는 개별 호가 원문·가격을 넣지 않는다. `report --size-scale 100`은 공식 제공자의 과거 round-lot 단위를 참고한 **추가 해석**으로 새 독립 확인이나 실전 승격 근거가 아니다. 두 실행의 `event_ledger/results/verdicts`를 비교하며, 메타데이터가 더해진 전체 JSON의 해시가 같다고 주장하지 않는다. [최종 결과](../../docs/EARNINGS-ANNOUNCEMENT-RESULT-2026-10-17.md)에서 두 경로는 현재 실전 불채택으로 닫았다. 당시 실제 API 수신·정산 비용·역사 잔량의 소급 변환과 실제 체결은 인증하지 않아 실주문 적격은 false다. 기록된 응답 건수는 보존된 호출의 수이며 청구서나 보존하지 않은 진단/중단 요청의 합계가 아니다.

## 2026-10-11 일봉 판단 재생 기반

`.venv/bin/python -m scripts.measure.research_daily_foundation --window 2026q2early-v5 --horizon 5`는 보존된 KRX 원문과 가격 전용 판단 기록을 연구용 판단·현금 재생에 연결한다. `--window`는 이미 열람한 2024년 세 구간과 2025~2026년 네 구간, `--horizon`은 5·20만 받는다. 각 조합에서 기존 `holding_horizon_portfolio_lab`의 체결 순서·가격·수량과 비용 후 결과가 일치하는지 `legacy_replay_parity`로 보고한다. 당시 순위와 공급자 도착 시각이 없는 입력은 가정으로 표시한다. 이 보고서는 등록 판정·운영 주문·새 전략 수익 증거가 아니다. 세부 계약과 남은 조건은 [엔진 재설계](../../docs/ENGINE-REBUILD-BLUEPRINT-2026-10-11.md)에 있다.

`--sensitivity`를 더하면 같은 개발 입력에 비용 25/75/120bp, 불리한 시가 체결, 부분체결·미체결 시나리오를 적용한다. 손익이 없거나 더 좋아지는 시나리오도 숨기지 않는다. 이는 **가격만 사용한 가상 계산**이며 KRX 일별 원본은 기업행사·배당의 완전한 원천이 아니다. 검증된 총수익, 독립 표본 성과, 실제 주문 체결로 읽지 않는다.

`.venv/bin/python -m scripts.measure.research_entry_edge_audit --output data/research/<새 파일>.json`은 같은 동결 7개 창에서 **반복 BUY 날짜**와 **첫 매수 회차**를 분리해 h1·h5의 같은 날 후보군 대비 결과를 보고한다. KRX 원문·파일럿·결손 장부 SHA를 검사하고 누락 날짜가 있으면 중단한다. 원본을 덮어쓰지 않으며 실전 8팩터 판정이나 주문 성과를 뜻하지 않는다. [2026-10-14 결과](../../docs/RESEARCH-ENTRY-EDGE-2026-10-14.md)를 참조한다.

### 과거 기업행사 원문 보존·검수

`acquire_historical_sources.py`의 `dart-actions`는 한 발행사의 최대 32일 접수 목록과 모든 원문 ZIP을 보존한다. `kis-dividend`/`kis-rev-split`과 `kis-bonus-issue`/`kis-cap-dcrs`/`kis-merger-split`/`kis-paidin-capin`/`kis-list-info`는 한 종목의 최대 32일 과거 KSD 계열 일정 응답을 요청 상한 내에서 읽기 전용으로 보존한다. 새 ZIP을 `data/research/`에 저장하며, 기존 파일을 덮어쓰지 않는다. `--end`는 등록 보호 시작 전이어야 한다. 실제 키는 로컬 환경 설정에서 읽고 출력하지 않는다. 새 다섯 유형은 **원문 일정 보존만** 지원한다. `inspect-kis`의 정산 사실 추출은 기존 배당·액면교체에 한정되며 합병 비율·효력일·지급일을 이 일정에서 확정하지 않는다.

```bash
.venv/bin/python scripts/measure/acquire_historical_sources.py dart-actions \
  --corp-code 00126380 --start 2024-04-30 --end 2024-04-30 \
  --output data/research/dart-actions-00126380-2024-04-30-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py kis-dividend \
  --ticker 005930 --start 2024-03-01 --end 2024-03-31 \
  --output data/research/kis-dividend-005930-2024-03-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py verify-actions \
  --output data/research/dart-actions-00126380-2024-04-30-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py inspect-dividend \
  --receipt 20240430800061 \
  --output data/research/dart-actions-00126380-2024-04-30-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py compare-dividend \
  --dart-archive data/research/dart-actions-00126380-2024-04-30-v1.zip \
  --receipt 20240430800061 \
  --output data/research/kis-dividend-005930-2024-03-v1.zip
```

`inspect-kis --output <KIS ZIP>`은 보존한 일정의 정규화 사실을 출력한다. 조회일 의미·정확한 배당락/효력일·당시 공개시각·실제 지급이 확인되지 않은 사실은 반드시 미확인으로 남긴다. 해시 일치나 두 일정의 필드 일치는 행사 전수 보장·총수익 적격을 뜻하지 않는다. 세부 범위와 실제 대조표는 [원천 완전성 판단](../../docs/RESEARCH-SOURCE-COMPLETENESS-2026-10-11.md)에 기록했다.

합병·분할 일정의 실제 두 양성 대조와 비율 정밀도 경고는 [KIS 일정 교차 검수](../../docs/RESEARCH-KIS-MERGER-SCHEDULE-2026-10-14.md)에 있다. 예정 단주 지급일은 실제 지급일이 아니며 KIS와 SEIBRO/예탁원 공공 API는 독립 원천 셋으로 세지 않는다.

세 상장폐지/합병의 **선별 KIND 원문 8건**은 [고정 목록](../../docs/research-action-kind-review-plan-2026-10-11.json)에서 읽기 전용으로 수집한다. HTTPS KIND 공식 경로·발행사 문자열·응답 크기를 검사하고 ZIP 원문 해시를 보존한다. 이것은 KIND 공시 전수 조회가 아니며 기존 결손표의 `partial` 상태를 바꾸지 않는다.

```bash
.venv/bin/python scripts/measure/acquire_historical_sources.py kind-reviewed \
  --source-plan docs/research-action-kind-review-plan-2026-10-11.json \
  --output data/research/kind-action-review-2026-10-11-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py verify-kind \
  --output data/research/kind-action-review-2026-10-11-v1.zip
```

재수집은 새 출력명으로만 가능하다. 기존 ZIP은 덮어쓰지 않으며, 후일 웹 원문이 정정되어 바이트가 바뀌면 해시가 달라진 **새 관측**으로 기록해야 한다. 사례별 근거와 아직 확인하지 못한 정산은 [후속 검수](../../docs/RESEARCH-ACTION-GAP-AUDIT-2026-10-11.md#2026-10-11-후속-원문-검수--위-초기-조사표의-보완)에 있다.

`research_action_gap_ledger.py`는 기존 7개 개발 창의 시가총액 상위 200 **후보 전체**를 대상으로 종목별 기업행사 결손표를 재생한다. 코드에 새 전략 수익률이나 매매 규칙은 없다. 원문 ZIP 네 개가 로컬에 있는 경우 재현 명령은 다음과 같다. 출력은 새 파일만 허용하며 저장소의 [보존 결과(JSON Lines)](../../docs/research-action-gap-ledger-2026-10-11.jsonl)와 [해석](../../docs/RESEARCH-ACTION-GAP-AUDIT-2026-10-11.md)을 비교할 수 있다. Git에 들어 있는 JSONL은 원문 ZIP의 대체가 아니며 이 명령은 키나 네트워크를 사용하지 않는다.

```bash
.venv/bin/python -m scripts.measure.research_action_gap_ledger \
  --action-archive data/research/dart-actions-00126380-2024-04-30-v1.zip \
  --action-archive data/research/dart-actions-00126380-2024-05-v1.zip \
  --action-archive data/research/kis-dividend-005930-2024-03-v1.zip \
  --action-archive data/research/kis-rev-split-042700-2022-04-v1.zip \
  --reviewed-cases docs/research-action-reviewed-cases-2026-10-11.json \
  --output data/research/action-gap-ledger-repeat.jsonl
```

`research_action_input_readiness.py`는 위 보존 JSONL과 KRX 원문 두 패널의 해시·일별 후보를 다시 맞춘 뒤, 종목별 253개 **연속 유효 종가**가 없어서 파일럿이 제외한 후보일을 계산한다. 2025 Q4~2026-06 네 창은 동결한 파일럿의 제외 수와 비교한다. [검수 해석](../../docs/RESEARCH-ACTION-INPUT-READINESS-2026-10-12.md)과 [결과 JSON](../../docs/research-action-input-readiness-2026-10-12.json)은 기업행사 커버리지나 총수익 인증이 아니다. 재현 출력은 새 경로를 지정한다.

남은 17종목의 첫 가격 전 공백 유형은 [KIND 대조표](../../docs/RESEARCH-ACTION-LISTING-CENSUS-2026-10-12.md)와 `research_action_listing_census.py`로 확인한다. 스크립트는 동결 KRX 종목명·첫 종가일과 참조 URL·원문 보존 계획의 일치를 검사하지만, 사이트의 과거 내용이나 기업행사 권리 정산을 인증하지 않는다. `/external/` 9건과 `/listinvstg/` 6건은 서로 다른 ZIP으로 선택 보존했다. 나머지 2건은 현재 직접 요청 시 빈 화면 틀이라 URL 참조만 남겼다. 상장 이력 수집은 다음처럼 실행한다.

```bash
.venv/bin/python scripts/measure/acquire_historical_sources.py kind-listing-reviewed \
  --source-plan docs/research-action-listing-history-plan-2026-10-12.json \
  --output data/research/kind-listing-history-2026-10-12-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py verify-kind \
  --output data/research/kind-listing-history-2026-10-12-v1.zip
```

```bash
.venv/bin/python -m scripts.measure.research_action_input_readiness \
  --output data/research/action-input-readiness-repeat.json
```

선별한 이전상장·재상장 공시 원문 3건의 수집/검증은 다음처럼 따로 수행한다. 반환 상태는 `selected_originals_not_action_complete`다.

```bash
.venv/bin/python scripts/measure/acquire_historical_sources.py kind-reviewed \
  --source-plan docs/research-action-listing-review-plan-2026-10-12.json \
  --output data/research/kind-listing-review-2026-10-12-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py verify-kind \
  --output data/research/kind-listing-review-2026-10-12-v1.zip
```

더존비즈온 `012510`의 2026-07 가격 소멸은 [매매정지·주식교환 계보](../../docs/RESEARCH-ACTION-DOUZONE-DELISTING-2026-10-12.md)로 따로 검수한다. 아래 두 KIND 원문을 고정 보존하고 KRX 원본과 함께 재생한다. **실제 교부금 수령은 확인하지 않는다.**

```bash
.venv/bin/python scripts/measure/acquire_historical_sources.py kind-reviewed \
  --source-plan docs/research-action-douzone-review-plan-2026-10-12.json \
  --output data/research/kind-douzone-review-2026-10-12-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py verify-kind \
  --output data/research/kind-douzone-review-2026-10-12-v1.zip
.venv/bin/python -m scripts.measure.research_action_douzone_lineage
```

## 왜 `pit6.py` 가 있나 (먼저 읽을 것)

동결된 가격 전용 첫 BUY 파일럿의 h20 구간에 겹친 KRX 상장주식수 변화는 [연구 전용 우선순위](../../docs/RESEARCH-ACTION-HELD-CHANGE-PRIORITY-2026-10-12.md)로 재생한다. 이 큐는 실제 보유·권리·총수익의 증명이 아니다. 기존 동결 ZIP·파일럿·결손표의 SHA가 달라지면 실패한다. 결과를 새 출력명으로 생성한다.

```bash
.venv/bin/python -m scripts.measure.research_action_holding_change_priority \
  --output data/research/holding-change-priority-repeat.json
.venv/bin/python scripts/measure/acquire_historical_sources.py verify-kind \
  --output data/research/kind-held-change-review-2026-10-12-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py verify-kind \
  --output data/research/kind-held-change-followup-2026-10-12-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py verify-kind \
  --output data/research/kind-sksquare-change-2026-10-12-v1.zip
.venv/bin/python scripts/measure/acquire_historical_sources.py verify-kind \
  --output data/research/kind-final-held-change-2026-10-12-v1.zip
```

`store.pit_fund_scores` 는 `fundamentals.json` 에 시가총액이 없으면 **발행주식수 근사에서
막힌다**(`"시가총액·현재가가 없어 발행주식수를 근사할 수 없다"`). 로컬 캐시가 낡으면 6팩터
PIT 하네스를 **아예 못 돌린다** — KRX 인증이 필요한 수집을 먼저 해야 한다.

`pit6.py` 는 `universe_history.json` 의 **PIT 시총 앵커**(305종목 × 60스냅샷)로 주식수를
역산해 같은 경로를 탄다. 프로덕션에서는 `fundamentals.json` 이 신선해서 이 우회가 필요 없다.
아래 스크립트 대부분이 이걸 import 한다.

| 스크립트 | 무엇을 재나 | 결과 |
|---|---|---|
| `pit6.py` | (공용) 로컬 6팩터 PIT 점수 조립 | — |
| `ic_corr.py` | 한·미 횡단면 IC 상관 | **ρ = +0.301** [CI +0.096, +0.481] → pooled 시 −4.7개월 |
| `tick_freq.py` | 표본 주기별 조기청산율 | 1표본 79.7% · 13 87.9% · 78 **89.5%** |
| `lossmaker.py` | 적자 더미의 국면별 IC | 전체 **−0.0294 (t −2.93)** · 전 국면 음수 |
| `bias.py` | 커버리지별 \|점수\| (재정규화 편향) | 0.67에서 **1.000** — 비단조 |
| `denom_ab.py` | 분모 A/B (시드 5개) | 81.9% → 68.2% |
| `substitution.py` | 분모 교체 픽 분해 | **24픽 · Welch t 0.57** ← 결론을 뒤집은 측정 |
| `window_mix.py` | 분모별 매수권 커버리지 구성 | 121건(3.5%) → 0건 |
| `growth_decomp.py` | 성장 가중 픽 분해 | 교체 41~55% · t −0.11/−1.04/−1.13 |
| `timing.py` | 국면 익스포저 타이밍 기여 | **−14.6%p · t −1.12**(유의 아님) |

## 하네스 노브로 흡수된 것 (스크립트 불필요)

이제 `sigdesk harness` 플래그로 돈다:

| 옛 스크립트 | 대체 |
|---|---|
| 청산 규칙 A/B | `--exits {conservative\|balanced\|aggressive}` |
| 트레일링 arming | `--legacy-trailing` |
| σ 스케일 청산 | `--sigma-exits` |
| 재정규화 분모 | `--full-denominator` |
| 장중 표본 | `--intraday-samples {1\|13\|78}` |
| 픽 단위 분해 | `harness.compare_picks(panel, scores_a, scores_b, cfg)` |

## 주의

- **모두 탐색 실행이다.** 보드 정본이 아니고 사전등록 look이 아니다.
- 로컬 캐시 기준(국내 시세 ~2026-08-14 · 미국 ~2026-07-24)이라 프로덕션과 다르다.
- `panel.pkl` 같은 중간 캐시는 커밋하지 않는다.

## 2026-10-17 매출·가이던스 독립 표본과 호가 조건 차단

[고정 계획](../../docs/EARNINGS-GUIDANCE-VALIDATION-2026-10-17.md)과 [원문 대조·결과](../../docs/EARNINGS-GUIDANCE-RESULT-2026-10-17.md). 기존2022/2023 EPS 정책의 손실12건·NVDA 이익3건을 대조한 뒤, 아직 가격 결과를 검사하지 않은2020개발/2021확인64발표를 고정했다. 10분 뒤 EPS/매출 확인·알려진 하향 전망 회피, 같은 시점 EPS-only/SPY/현금·비용·지연·수량·회사 제외 재선택을 구현했다. **신규 수익성은 과거 `?` 호가 조건으로 차단**됐으며, 정상 `R`로 바꾸거나 손실0/수익0으로 계산하지 않는다. 이 문제는 더 많은 인접초 요청으로 풀지 않는다.

```bash
# 로컬에 보존한 실제 원문·고정 코드가 있어야 재생 가능. 새 출력명 사용.
.venv/bin/python -m scripts.measure.earnings_guidance_delivery replay \
  --archive data/research/earnings-guidance-source-bundle-2026-10-17-v3.zip \
  --report data/research/earnings-guidance-repeat.json

# 같은64기사 입력·SIP 원문으로 데이터 추가 필요 여부 검사.
# 현재는 source_quality.blocked=true, requests=[]로 불필요 수집을 멈춘다.
.venv/bin/python -m scripts.measure.earnings_guidance_validation needs \
  --input data/research/earnings-guidance-inputs-2026-10-17-v4.json \
  --sources data/research/earnings-guidance-quotes-2026-10-17-*.json \
  --output data/research/earnings-guidance-needs-repeat.json

# 기존 실험 손익 분해: 원래 결과를 덮어쓰지 않는다.
.venv/bin/python -m scripts.measure.earnings_guidance_audit attribution \
  --source data/research/earnings-announcement-report-2026-10-17-v3.json \
  --output data/research/earnings-guidance-attribution-repeat.json
```

재생은 ZIP의 원문/입력/코드·계획 해시를 검증한다. 이전 보고서와 `event_ledger/results/leave_one_issuer_out/checks/verdict/source_quality`를 대조한다. 현재2020/2021 원문의 조건을 복원하거나 정상 조건이 보존된 동기간 NBBO를 확보하기 전 수익성 완료·채택을 주장하지 않는다. 공개 요약에는 거래별 실제 호가 가격을 내보내지 않는다. 실제 API 도착·원본 기사 버전·계좌 정산 비용은 별도 제한이고 운영 주문/점수/등록 잠금은 바꾸지 않는다.
