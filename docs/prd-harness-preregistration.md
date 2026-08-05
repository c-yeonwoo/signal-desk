# 하네스 사전등록 · 설정 주입 · 판정 이력

> 상태: **구현 완료(단계 0~8)** | 작성: 2026-08-05 | 기준 문서 (결정 D1~D11 · 미해결 Q6 1건)
> 구현: `signals/harness.py`(실효 기간) · `db.py`(`harness_runs`) · `prereg.py`(신규) · `store.py`
> (`run_harness` 설정 주입 · `run_preregistered` · `harness_board`) · `api.py`(엔드포인트 3 · 스케줄러) ·
> `cli.py`(`--preregistered`) · `web/index.html`(판별력 패널 · 실험 카드) · `docs/preregistered.toml`
> 검증: `tests/test_redteam.py` 불변식 9개 + 전체 **818 passed · 실패 0**
> 진단 출처: 정합성 진단 2026-08-04~05 · Now 항목 **N3**
> 척도 승계: [north-star-selection.md](north-star-selection.md) — V1 시그널 판별력 > V2 검증 워크플로 속도 > V3 읽을 수 있음

---

## 문제

증명 장치가 증명 대상을 재고 있지 않다. 다섯 갈래로 확인됐다.

**P1 — 하네스에 설정을 넣을 방법이 없다.**
`store.run_harness()`가 `HarnessConfig`를 `signal_config` 인자 없이 생성한다(`store.py:1464-1467`).
그러면 `default_factory=SignalConfig`(`harness.py:59`)가 걸려 **`engine.py` 상단 하드코딩 상수**를 검사한다.
반면 시그널 탭·봇·숏폼은 `signalcfg.effective_config()`를 쓴다(`api.py:176` · `bot.py:109` · `shortform.py:82`).
지금은 `kv:signal_config = {}`라 두 경로가 우연히 일치하지만, **관리자 UI에서 가중치를 바꾸는 순간
판정은 그 변경을 재지 않는다.** API body는 `{market, trials}`뿐이고(`index.html:5979`)
CLI에도 가중치 플래그가 없다(`cli.py:124-134`).
→ 그래서 H1(technical 0.35→0)·H2(momentum=0)는 **`engine.py` 소스를 편집하고 터미널에서** 돌렸고,
결과는 `.gitignore` 밑(`data/cache/harness_hypotheses_*.json`)이며 재현 스크립트가 레포에 없다.

**P2 — 판정이 덮어쓰기다.**
`save_harness_last()`가 `harness_last.json`을 통째로 교체하고(`store.py:1426-1434`) `db.py`에 harness 테이블이 없다.
**설정 변경에는 이력 메커니즘이 있는데**(`signalcfg.append_history()`, 호출처 3곳 —
`signalcfg.py:50` · `api.py:3513` · `brain_proposals.py:369`) **판정 결과에는 없는 비대칭**이다.
가장 중요한 산출물이 가장 덜 보존된다.

**P3 — 스윕의 마지막 조합이 정본을 덮고, 다중검정 보정이 없다.**
`cli.py:184`가 combos 루프 **안에서** `save_harness_last()`를 호출한다. 기본 스윕은
`top_pct ∈ {1,3,5,10} × hold ∈ {5,20}` = **8조합**이므로 판별력이 전혀 없어도 하나가 95%를 넘을 확률이
**33.7%**다. `cli.py:203-208`에 그 확률을 찍는 경고는 있지만 **채택을 막지 않고**, 저장된 payload에
가중치가 없어 사후에 어느 조합이었는지 식별할 수 없다.

**P4 — 표본 게이트가 미참여 기간을 표본으로 센다.**

> **정정(2026-08-05 구현 중).** 초안에서 “`periods`가 전체 리밸런스 횟수(≈218)라 PIT에서 214회가
> 매수 0건이어도 통과한다”고 썼는데 **틀렸다.** `run()`은 PIT 모드에서 **이미 스냅샷 날짜가 있는
> 인덱스만 남긴다**(`harness.py:376-380`) — 로컬 실측으로 PIT 10거래일·hold=5면 `periods = 2`다.
> 즉 `periods`가 부풀지는 않는다. 아래가 실제 문제다.

1. **PIT 완화가 표본 요건을 6배 느슨하게 했다.** `min_periods = min(cfg.min_periods, 5)`
   (`harness.py:418`)라 PIT은 **실효 5기간 = 25거래일**만 있어도 판정을 냈다. 사전등록 요건
   (final 실효 30기간)과 정면으로 어긋난다.
2. **필터된 기간 안의 매수 0건이 표본으로 세어졌다.** 게이트·문턱 때문에 후보가 비는 기간이
   `periods`에 포함된다. 로컬 실측: PIT `periods 2` 중 최악 위상의 실효는 **1**이다.
3. **가격 경로에서는 미참여가 경고로만 남았다.** 실측 `90/1095기간이 매수 0건`인데
   `harness.py:407-409`의 경고 문구뿐이고 판정에는 반영되지 않는다. 같은 파일의
   `weak_factors`는 차단인데 이쪽은 아니다.

**P5 — 판정이 통과 가능한 최소 격자점에 서 있다.**
`percentile = better/trials × 100`(`harness.py:395`), 문턱 `>= 95`(`:476`).
자동 경로는 `trials=40`(`store.py:1447`), CLI는 `100`(`cli.py:128`) — 같은 판정이 경로마다 해상도가 다르다.
로컬 08-04 실행은 **60시행 중 57승 = 95.0**으로 문턱을 만족하는 **최소 격자점에 정확히 걸쳐 있고,
대조군 하나만 뒤집히면 56/60 = 93.3 = 판정 불가**가 된다. 프로덕션 p90은 40시행의 36/40이다.
덧붙여 전략 평균 누적 185.1%는 **무작위 p95인 193.2%보다 낮다** — `_verdict`가 요구하는 것은
`phase_min > random_median`(98.6 > 82.3)뿐이므로 “최악 위상도 우위”는 무작위 **중위**를 넘긴 것이다.

---

## 전달 가치

**V1(판별력)** — 판정이 실제 운영 엔진 설정을 재고, 그 판정이 사전에 정한 규칙으로만 확정되며,
무엇을 언제 어떤 설정으로 쟀는지 사후에 재구성할 수 있다.
**V2(검증 속도)** — 가설 → 검사 → 판정의 가운데 단계가 제품 안으로 들어온다. 소스 편집이 없어진다.

이 PRD는 V3(화면 가독성)을 목표로 하지 않는다 — 진단의 N4·X4가 그 몫이다.

---

## 대상 사용자와 시나리오

사용자는 **본인 1명**(R&D 랩, 외부 유저 0). 시나리오 셋:

**S1 — 가설을 시험한다.** "rank 창을 6자리에서 12자리로 넓히면 판별력이 오르나?"
→ 관리자 실험 카드에서 `rank_top_pct 3.0 → 6.0` 오버라이드를 넣고 탐색 실행.
결과는 **이력에만** 쌓이고 보드는 안 움직인다. 소스 편집 없음.

**S2 — 진짜로 채택할 것을 등록한다.** 탐색에서 유망해 보이면 `docs/preregistered.toml`에
조합·가설·표본 요건·판정 기준·통과 시 무엇을 바꿀지를 적고 **커밋한다**.
그 뒤 자동 실행이 요건 진척만 갱신하고, 요건 충족일에 **단 한 번** 판정하고 동면한다.

**S3 — 6개월 뒤의 내가 되짚는다.** "momentum 0.30은 왜 이렇게 됐나?"
→ `harness_runs` 이력에서 그날의 설정·조합·백분위·실효 기간과 사전등록 id를 읽는다.
사전등록이 없던 실행은 `preregistered_id = null`로 남아 **정본이 아니었음이 드러난다**.

---

## 목표

| 지표 | as-is | to-be |
|---|---|---|
| 하네스가 검사하는 설정 | `engine.py` 하드코딩 기본값 | 호출자가 넘긴 설정(기본은 `signalcfg.get_config()` — 소스 기본값 + kv 오버라이드) |
| 판정 이력 | 1슬롯 덮어쓰기 | append-only, 설정·조합·실효 기간·사전등록 id 포함 |
| 보드 정본을 정하는 규칙 | 마지막 실행 | 사전등록된 조합 + 요건 충족 1회 확정 |
| 실효 기간 미달 시 | 경고(통과) | **차단**(판정 불가) |
| 다중검정 | 경고 문구 | Šidák 보정된 문턱을 판정에 실제 적용 |
| 소스 편집이 필요한 실험 | 전부 | 없음 |
| 설정의 진실 | 소스 상수·kv·사전등록이 서로 모를 수 있다 | 셋을 비교해 불일치를 화면에 드러냄(F12) |

**성공 측정**: (a) `test_redteam`에 이 PRD의 불변식 **9개**가 검사로 존재하고 통과한다.
(b) H1·H2를 소스 편집 없이 재현해 이력에 남길 수 있다. (c) `/api/proof`의 `A.harness`가
`판정 보류 · 요건까지 n일`을 정확한 숫자로 낸다.

---

## 비목표

- **판정 게이트(판정 불가면 파라미터 변경 거절)는 이 PRD의 범위가 아니다** — 진단의 N2다.
  단 N2가 쓸 계약(`config_hash`, `verdict_locked_at`, `effective_periods`)은 여기서 만든다.
- 시그널 탭 판별력 배너·시각 강등(N4·X4) 제외.
- IC 계산을 횡단면으로 바꾸는 것(X1) 제외. 다만 요건 정의에 PIT 날짜 수를 쓰므로 **인터페이스는 맞춰 둔다**.
- 새 팩터·새 게이트·가중치 실제 변경 없음. **이 PRD는 무엇도 채택하지 않는다.**
- OOS 홀드아웃(L1)·Hansen SPA(L2)·DSR(L3) 제외 — Later.
- 가격 하네스를 삭제하지 않는다. 탐색 도구로 남기고 **보드에서만 내린다**.

---

## 요구사항 — 기능

### F1. 사전등록 파일 `docs/preregistered.toml` (git 커밋)
코드가 읽고, 변경은 커밋 이력에 남는다. 스키마:

```toml
# docs/preregistered.toml — 사전등록. 코드가 읽고, 변경은 커밋 이력에 남는다.
# tomllib(표준 라이브러리, py3.11+) 로 읽는다 — 새 의존성 0.
# Šidák n = [[looks]] 중 score_source="pit" 인 항목 수. lock 상태와 무관하게 고정(F9-a).

[base]
family       = "pit-8factor-rank3-hold5"   # 같은 가설의 2회 관측
score_source = "pit"                        # 정본은 pit만 허용(F10)
market       = "kr"

# signalcfg.FIELDS(15개) + MODE_FIELD 전진. 값은 2026-08-05 engine.SignalConfig 실측과 일치.
# weight_qualitative 는 FIELDS 에 없다(KB 정성은 veto/shadow) → 사전등록 대상 아님.
[base.config]
weight_technical      = 0.0     # H1로 0
weight_fundamental    = 0.30
weight_valuation      = 0.15
weight_reversion      = 0.20
weight_flow           = 0.20
weight_quality        = 0.15    # 커버리지 0% — 측정 불가 상태로 실려 있다
weight_momentum       = 0.30    # 최대 가중. 코드 주석상 "라이브 IC로 확정" 예정 = 잠정
weight_short          = 0.15    # 커버리지 9.1%
strong_buy_threshold  = 2.0
buy_threshold         = 1.2
sell_threshold        = -1.2
strong_sell_threshold = -2.0
regime_adaptive       = 1.0
rank_top_pct          = 3.0
rank_min_score        = 1.2
selection_mode        = "rank"

[base.harness]
hold     = 5
cost_pct = 0.25
trials   = 200      # F9-c — 격자 0.5%p. 40이면 문턱 97.47%를 표현조차 못 한다
exposure = false    # D10 — 하네스는 100%. 라이브 익스포저와 다르다는 사실만 화면에 낸다

[[looks]]
id            = "pit-8factor-rank3-hold5-interim"
role          = "interim"
registered_at = "2026-08-05"
hypothesis    = """
PIT 8팩터 종합점수 상위 3%(6자리)·5일 보유 랭킹은 라벨 치환 대조군보다 상위에 있다.
이 관측은 중간 판독이며 채택 근거가 아니다 — 통과해도 final까지 가중치를 바꾸지 않는다.
hold=5는 실측 채점 지평(20거래일)과 다르다. hold=20은 실효 30기간에 600거래일이 필요해
현실적이지 않아 5로 등록한다(D7).
Šidák n=2(문턱 97.47%)는 독립 다중검정이 아니라 같은 가설의 2회 순차 관측에 대한
보수적 대용이다 — 두 관측은 양의 상관이므로 필요한 것보다 엄격한 쪽으로 틀린다(F9-a).
"""
[looks.requirement]
min_effective_periods = 12    # periods - empty_periods (F6)
min_pit_dates         = 60    # 2026-08-05 기준 19일 → +41거래일

[looks.decision]
if_pass = "중간 판독 통과. 채택 보류 — final 요건까지 대기. 가중치 변경 게이트는 열지 않는다."
if_fail = "중간 판독 실패. 그래도 final까지 대기한다(표본 부족과 판별력 부재를 구분 못 하므로)."

[[looks]]
id            = "pit-8factor-rank3-hold5-final"
role          = "final"
registered_at = "2026-08-05"
hypothesis    = "같은 가설의 확정 관측. 이 결과만 정본이다."
[looks.requirement]
min_effective_periods = 30    # 기존 harness.min_periods 를 그대로 존중
min_pit_dates         = 150   # 2026-08-05 기준 19일 → +131거래일

[looks.decision]
if_pass = "종합점수를 순위 근거로 확정. 가중치 변경 게이트(N2)를 연다."
if_fail = "종합점수를 순위 근거로 쓰지 않는다. rank 창·게이트 재설계로 이동."
```

> **형식 결정(2026-08-05)**: 초안은 YAML이었으나 **PyYAML이 이 레포 의존성에 없다**(레포는 외부 의존성
> 최소를 지킨다). `tomllib`은 3.11+ 표준 라이브러리이고 `requires-python = ">=3.11,<3.14"`이므로
> **TOML로 확정**한다 — 새 의존성 0, 읽기 전용 파서.
> 대가: TOML에 앵커가 없어 F9-b의 설정 공유를 `[base]` 상속으로 구현한다(look이 `config`를 생략하면 `[base.config]`).

**요건 도달 추정** — 코드가 실제 휴장일 달력으로 세지만, 거래일 21일/월 가정 시:

| look | 실효 기간 | PIT 거래일 | 현재 19일 기준 남음 | 대략 시점 |
|---|---|---|---|---|
| interim | ≥ 12 | 60 | +41거래일 | **2026-10월경** |
| final | ≥ 30 | 150 | +131거래일 | **2027-02월경** |

`hold=5`는 실측 채점 지평(20거래일)과 다르다 — 그 불일치를 `hypothesis`에 남긴다(D7).

- 파일이 없거나 파싱 실패면 보드는 `판정 불가 · 사전등록 없음`을 이유와 함께 낸다(조용한 0 금지).
- `id` 중복·`score_source = "price"`(정본) 은 로드 시 거절하고 이유를 남긴다.

### F2. `HarnessConfig`에 설정을 주입한다
`store.run_harness()`에 `signal_config: SignalConfig | None = None` 인자를 추가하고
`HarnessConfig(..., signal_config=...)`로 넘긴다. **기본값은 `signalcfg.get_config()`** —
즉 인자를 안 주면 하드코딩 기본값이 아니라 **소스 기본값 + kv 오버라이드**를 검사한다(P1의 근본 수정).
`effective_config()`가 아니라 `get_config()`인 이유: `effective_config`는 국면 적응(익스포저·문턱)을
얹는데 **D10으로 하네스는 익스포저를 안 쓰기로 했고**, `rank` 모드에서 문턱은 어차피 불변이다.
또 `effective_config`는 `regime`·`macro`·`flow`를 인자로 받아 `api.py`에서 조립되므로
`store`가 그것을 부르면 순환 import가 된다.
`hz.run()`은 변경하지 않는다(이미 `cfg.signal_config`를 쓴다).

**부수 불일치도 같이 드러낸다 — 익스포저.**
`effective_config()`는 국면 적응을 적용한다(`signalcfg.py:175`). `rank` 모드에서는 문턱이 불변이지만
**익스포저는 바뀐다** — 프로덕션 화면이 `약세 국면 — 기준 익스포저 40%`를 보여주고 있다.
반면 `HarnessConfig.use_exposure`는 기본 `False`이고 자동 경로도 `exposure=False`로 부른다
(`store.py:1447-1448`). 즉 **라이브는 40% 익스포저로 돌고 하네스는 100%로 잰다.**
가중치 불일치와 별개인 두 번째 불일치다.
`preregistered.toml`의 `harness.exposure`를 **명시 필수 필드**로 두고, `effective_config`의
`adapt` 결과와 다르면 보드에 `익스포저 조건이 라이브와 다르다(하네스 100% / 라이브 {n}%)`를 낸다.
**D10으로 확정: 하네스는 100% 유지.** 하네스의 귀무가설은 “점수에 미래 정보가 없다”이고
익스포저는 그 가설과 무관한 사이징 레이어다 — 섞으면 국면 룰의 오류가 판별력 판정에 스며들어
무엇이 실패했는지 못 가른다. **다르다는 사실만 화면에 낸다.**

### F3. CLI에 실험 축을 연다
`sigdesk harness`에 셋 중 하나를 받는다(상호 배타):
- `--preregistered <id>` — 등록 항목을 그대로 실행. **정본 후보가 되는 유일한 경로.**
- `--config-json <path|->` — 임의 오버라이드로 탐색 실행. 이력에만 쌓인다.
- (없음) — `signalcfg.effective_config()`로 탐색 실행.

`--sweep`은 `--preregistered`와 함께 쓸 수 없다(에러). 스윕 결과는 **절대 `harness_last`를 쓰지 않는다**.

### F4. 관리자 실험 카드 (읽기/쓰기 분리)
관리자 `엔진` 축 안에 카드를 하나 추가한다. **판정을 읽는 카드와 값을 바꾸는 카드를 합치지 않는다.**
- **사전등록 목록**: id · 가설 1줄 · 요건 진척(`실효 기간 4/30 · PIT 19/150일`) · 상태(`보류` / `확정 2026-…` / `무효(설정 변경됨)`)
- **탐색 실행**: 8팩터 가중치 + `rank_top_pct`·`rank_min_score`·`selection_mode`·buy/sell 임계 입력 →
  `POST /api/harness/run`. 결과는 이력 표에만 추가되고 **보드 숫자는 안 바뀐다**(문구로 명시).
- **이력 표**: 최근 20건 — 실행 시각 · 사전등록 id(없으면 `탐색`) · score_source · 백분위 ·
  실효/전체 기간 · 판정 · 설정 해시.

### F5. 판정 이력 테이블 `harness_runs` (append-only)
`db.py`에 테이블을 추가한다. **UPDATE·DELETE 경로를 만들지 않는다.**

### F6. 실효 기간을 세고, 미달을 차단한다
`_run_phase`가 이미 `empty_periods`를 센다(`harness.py:256,290,301`). `run()`이
`effective_periods = periods − empty_periods`를 결과에 넣고, `_verdict`에 `periods` 대신
**`effective_periods`**를 넘긴다. `min_periods` 완화(`harness.py:418`)는 **삭제한다** —
효과가 없으면서 있는 것처럼 보이는 코드다.

미달이면 `판정 불가 · 실효 리밸런스 표본 {n}회 < 최소 {m}회 (전체 {periods}회 중 {empty}회는 매수 0건)`.

### F7. 요건 미충족 동안 보드가 보여줄 것
`/api/proof` `A.harness`에 `status: "pending" | "locked" | "invalidated" | "unregistered"`를 넣는다.
`pending`이면 판정 숫자 대신 **진척과 예상 도달일**을 낸다:
`판정 보류 · 실효 기간 4/30 · PIT 19/150일 · 거래일 기준 약 131일 남음`.
**요건 미충족 상태에서 백분위를 보드에 노출하지 않는다** — 노출하면 매일 보게 되고 그게 peeking이다.
(이력 표에서는 볼 수 있다. 이력은 진단용이고 보드는 판정용이다.)

### F8. 요건 충족일에 1회 확정하고 동면한다
자동 실행(`api.py:356-367`)은 **요건 진척 갱신만** 한다. 요건이 처음 충족된 실행에서
`verdict_locked_at`·`locked_percentile`·`locked_verdict`·`config_hash`를 기록하고 `status="locked"`으로 바꾼다.
그 뒤 실행은 이력에만 쌓이고 **보드 판정은 변하지 않는다**.
재판정은 `preregistered.toml`에 **새 id를 등록해 커밋**해야 한다(기존 항목 수정은 무효화로 처리, F11).

### F9. Šidák 보정을 판정에 실제 적용한다

**F9-a. `n`은 파일에 등록된 정본 look 총수로 고정한다 — `status`와 무관하다.**
초안에서 `n = status != "locked"인 항목 수`로 썼는데, 그러면 interim이 확정되는 순간
`n`이 2 → 1로 줄어 **final의 문턱이 97.47% → 95%로 저절로 느슨해진다.** 사후 완화이므로 금지한다.
`n = len(looks where score_source == "pit")` — 파일이 바뀌지 않으면 문턱도 바뀌지 않는다.

per-comparison 유의수준 `α₁ = 1 − (1 − 0.05)^(1/n)`, 판정 문턱 백분위 `= (1 − α₁) × 100`:

| n | 문턱 | 우리 케이스 |
|---|---|---|
| 1 | 95.00% | |
| **2** | **97.47%** | ← interim + final |
| 4 | 98.73% | |
| 8 | 99.36% | 기본 스윕(탐색 전용) |

**엄밀히 말하면 interim·final은 독립 다중검정이 아니라 같은 가설의 2회 순차 관측**(nested data)이다.
정확한 처리는 alpha-spending 경계지만, 두 관측은 **양의 상관**이므로 Šidák은 필요한 것보다
**보수적인 쪽**으로 틀린다. 1인 랩에서 그 방향의 오차는 받아들일 수 있으므로 Šidák n=2를
alpha-spending의 보수적 대용으로 쓴다. **이 근거를 `preregistered.toml` 주석에 남긴다** —
안 남기면 6개월 뒤에 "왜 97.47이지?"에서 시작해 95로 되돌린다.

**F9-b. 두 look의 `config`·`harness`가 같은지 검사한다.**
같은 가설의 2회 관측이므로 설정이 달라지면 순차 관측이 아니라 서로 다른 두 실험이다(그러면 Šidák
가정도 깨진다). `[base.config]`·`[base.harness]`를 상속하고(look이 생략하면 base), 로드 시 **정규화 후 해시를
비교해 불일치면 거절**한다. `hold`·`cost_pct`·`trials`·`exposure`도 같아야 한다.

**F9-c. `trials`를 200으로 고정한다.**
격자가 0.5%p가 된다. `trials=40`(현 자동 경로 기본, `store.py:1447-1448`)이면 격자가 2.5%p라
**문턱 97.47%를 표현조차 못 한다** — 97.5로 올림되어 실질 문턱이 달라진다. P5의 근본 수정이다.
`percentile`은 `round(better / len(totals) * 100, 1)`(`harness.py:395`)이므로 200시행이면
0.5%p 격자에 소수 1자리까지 표현된다.

판정 문구에 `문턱 97.47% (사전등록 2 look · Šidák 보정)`을 함께 낸다.

### F10. 가격 하네스를 보드에서 내린다
`score_source="price"`는 **정본이 될 수 없다**. 이유는 이 진단에서 실측됐다 —
`harness_last.json`의 `fired_pct = {technical 0.0, reversion 2.4, momentum 78.4}`,
즉 가격 재계산 경로는 fundamental·flow를 넣을 수 없어(룩어헤드) **사실상 모멘텀 단독 랭킹**을 잰다.
CLI 표와 이력에는 남기되 이름을 **`기술·모멘텀 부분집합 탐색`**으로 바꾼다.
`cli.py:184`의 `save_harness_last()` 호출은 **제거한다**(F3에서 `--preregistered`만 정본).

### F11. `config_hash`로 판정을 설정에 묶는다
확정 시점의 `config`(F1의 `config` 블록 + harness 파라미터)를 정규화해 SHA-256 앞 12자를 저장한다.
`signalcfg.effective_config()`가 그 해시와 달라지면 보드가 `status="invalidated"` ·
`무효 — 판정 이후 설정이 바뀌었다(재등록 필요)`를 낸다.
`preregistered.toml`의 해당 항목이 편집되어도 같다. **판정이 살아 있으려면 잰 것과 돌아가는 것이 같아야 한다.**

### F12. 사전등록 설정과 실제 설정의 3자 일치를 검사한다

> **초안 정정.** "`append_history()`가 구현돼 있는데 호출처가 없다"고 썼는데 **틀렸다.**
> 호출처는 셋 있다 — `signalcfg.py:50`(H1 kv 마이그레이션) · `api.py:3513`(관리자 저장) ·
> `brain_proposals.py:369`(제안 승인). `kv`에 `signal_config_history` **키가 아예 없는** 이유는
> 배선 누락이 아니라 **관리자 UI로 가중치를 바꾼 적이 한 번도 없기 때문**이다
> (`kv:signal_config = {}` 확인). H1(technical 0.35→0)은 `engine.py`의 `SignalConfig` **소스 기본값**을
> 바꾼 것이라 git이 이력이고, `signalcfg.history()`가 비어 있는 것이 정상이다.

바뀐 요구사항은 이것이다. 설정의 진실이 **세 곳**에 있다:
1. `engine.py` `SignalConfig` 소스 기본값 (H1이 바꾼 곳, git이 이력)
2. `kv:signal_config` 오버라이드 (관리자 UI, `append_history`가 이력)
3. `docs/preregistered.toml`의 `config` (사전등록, git이 이력)

**로드 시 셋을 비교해 불일치를 드러낸다.** 특히 (1)이 바뀌면 (3)이 조용히 낡는다 — H1이 정확히
그런 변경이었다. 불일치면 보드에 `사전등록 설정이 현재 엔진과 다르다 — 등록 갱신 필요`를 내고
정본 확정을 **차단**한다(F11의 `config_hash`와 같은 메커니즘, 대상만 다르다).

부수 작업: `signalcfg.set_dict()`·`reset()`이 `append_history`를 호출하는지 확인한다
(`api.py:3513`은 호출하지만 `set_dict` 자체는 아니므로, CLI·다른 경로가 생기면 새는 구조다).
호출을 `set_dict()` 안으로 내리는 것이 안전하다.

---

## 요구사항 — 비기능

- **결정론**: 같은 `(preregistered_id, 데이터 스냅샷)`이면 같은 백분위. `seed`는 `HarnessConfig`에 이미 고정.
  이력에 `price_data_to`(패널 마지막 거래일)와 `pit_dates`를 남겨 재현 조건을 식별할 수 있게 한다.
- **오프라인·결정론 테스트**: 새 코드는 외부 API 없이 테스트 가능해야 한다(루트 `conftest.py` 규약).
- **실행 시간**: 정본은 `trials=200`이라 수십 초~수 분. 기존 백그라운드 잡(`_harness_job`)을 그대로 쓴다.
  HTTP 타임아웃 경로를 새로 만들지 않는다.
- **로그**: 정본 확정은 `log.info`로 `id`·백분위·문턱·실효 기간을 남긴다. `print` 금지.
- **파일 크기**: `index.html`을 분할하지 않는다(진단 결론 — 빌드 스텝 없음이 V2에 기여).
  새 UI는 기존 관리자 카드 패턴을 따른다.
- **마이그레이션**: 현재 `harness_last.json`(08-03 · p90 · 판정 불가)을 `harness_runs`의 첫 레코드로
  흡수하고 `preregistered_id = null`, `note = "사전등록 이전 실행"`으로 남긴다. 파일은 읽기 호환만 유지한다.

---

## 데이터 · API 계약

### `harness_runs` (신규 테이블, append-only)
| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | INTEGER PK | |
| `ran_at` | TEXT | ISO8601 UTC |
| `preregistered_id` | TEXT NULL | null이면 탐색 실행 |
| `score_source` | TEXT | `pit` \| `price` |
| `market` | TEXT | `kr` \| `us` |
| `config_json` | TEXT | 검사한 `SignalConfig` 전체(정규화) |
| `config_hash` | TEXT | SHA-256[:12] |
| `harness_json` | TEXT | top_pct·hold·cost·trials·exposure·seed |
| `percentile` | REAL NULL | |
| `threshold_pct` | REAL | Šidák 보정 후 문턱 |
| `n_registered` | INTEGER | 보정에 쓴 조합 수 |
| `periods` | INTEGER | 전체 리밸런스 |
| `empty_periods` | INTEGER | 매수 0건 기간 |
| `effective_periods` | INTEGER | `periods − empty_periods` |
| `pit_dates` | INTEGER NULL | PIT 스냅샷 거래일 수 |
| `price_data_to` | TEXT | 패널 마지막 거래일 |
| `verdict` | TEXT | `harness._verdict` 라벨 |
| `verdict_why` | TEXT | |
| `is_locked` | INTEGER | 이 실행이 정본 확정이었나 |
| `warnings_json` | TEXT | `out["warnings"]` |

### `GET /api/proof` → `A.harness` (변경)
```json
{
  "ready": true,
  "status": "pending",
  "preregistered_id": "pit-8factor-rank3-hold5",
  "hypothesis": "PIT 8팩터 종합점수 상위 3%…",
  "score_source": "pit",
  "requirement": {
    "min_effective_periods": 30, "effective_periods": 4,
    "min_pit_dates": 150, "pit_dates": 19,
    "trading_days_remaining": 131, "eta_note": "거래일 기준 추정"
  },
  "threshold_pct": 95.0, "n_registered": 1,
  "percentile": null,
  "verdict": "판정 보류",
  "verdict_why": "실효 리밸런스 표본 4회 < 최소 30회 (전체 218회 중 214회는 매수 0건)",
  "locked": null,
  "last_run_at": "2026-08-05T…"
}
```
`status="locked"`이면 `percentile`·`verdict`·`locked.{at, config_hash}`가 채워지고
`requirement.effective_periods`는 확정 시점 값으로 고정된다.

### `POST /api/harness/run` (변경 · 관리자)
```json
{ "market": "kr", "mode": "explore",
  "config": { "weight_momentum": 0.20, "rank_top_pct": 6.0 },
  "harness": { "hold": 5, "trials": 40 } }
```
`mode: "preregistered"` + `id`를 주면 등록 항목으로 실행한다(`config`·`harness` 무시).
`mode="explore"`는 **`harness_last`를 쓰지 않는다**. 응답에 `run_id`와 `board_updated: false`를 넣는다.

### `GET /api/harness/runs?limit=20` (신규 · 관리자)
`harness_runs` 최신순. UI 이력 표가 쓴다.

### `GET /api/harness/preregistered` (신규 · 관리자)
`docs/preregistered.toml` 파싱 결과 + 각 항목의 현재 진척·상태. 파싱 실패 시 `{ok:false, reason}`.

---

## 엣지케이스와 실패 모드

| 케이스 | 기대 동작 |
|---|---|
| `preregistered.toml` 없음 / 파싱 실패 | 보드 `status="unregistered"` + 이유 문구. 자동 실행은 계속(이력만). **조용한 0 금지** |
| `id` 중복 | 로드 거절 + 어느 id인지 이유에 명시 |
| 정본에 `score_source: price` | 로드 거절 (F10) |
| `--preregistered`와 `--sweep` 동시 | CLI 에러, 실행 안 함 |
| 탐색 실행이 우연히 문턱을 넘음 | 이력에 남고 보드는 불변. 응답 `board_updated:false` |
| 요건 충족 전 사람이 수동 실행 | 진척만 갱신. 판정 확정 안 함 |
| 요건 충족일에 자동 실행 실패 | 이력에 실패 레코드 + `log.warning`. 다음 실행에서 재시도(확정은 여전히 첫 성공 1회) |
| 확정 후 데이터가 더 쌓임 | 보드 불변(동면). 이력에는 계속 쌓임 |
| 확정 후 `signalcfg` 변경 | `status="invalidated"` + `무효 — 판정 이후 설정이 바뀌었다` (F11) |
| 확정 후 `preregistered.toml` 해당 항목 편집 | 같음 — 무효화. 재판정은 새 `id` |
| PIT 스냅샷에 구멍(비연속) | `pit_dates`는 실제 날짜 수를 센다(연속 가정 안 함). `effective_periods`가 자동으로 낮게 나온다 |
| `effective_periods`가 요건 충족했는데 `pit_dates`는 미달(또는 반대) | **둘 다 충족해야 확정.** AND 조건 |
| 유니버스가 바뀌어 종목이 빠짐 | 기존 동작 유지(`build_panel`이 상장 이전을 None으로). 이력에 `price_data_to` 기록 |
| `weak_factors` 차단과 실효 기간 차단이 동시 | 둘 다 이유에 나열. 판정 불가 |
| 동일 `config_hash`로 재실행 | 정상. 이력에 별 레코드로 쌓이고 결정론이면 같은 백분위 |
| `trials` 상한(200) 초과 요청 | 200으로 clamp하고 이력에 실제 값 기록(현 `max(10, min(trials,200))` 유지) |
| DB 마이그레이션 실패 | 기동은 성공, 하네스 기능만 `ready:false` + 이유. 시그널·봇 경로는 영향 없음 |
| **interim 확정 후 final 문턱** | **97.47% 불변.** `n`은 파일 기준이므로 lock 상태에 영향받지 않는다(F9-a) |
| **interim 통과 · final 실패** | **final이 정본.** 채택하지 않는다. 보드는 final 판정을 내고 interim은 `중간 판독(참고)`으로 접어 표시 |
| **interim 실패 · final 통과** | final이 정본. 채택한다. interim 실패는 표본 부족과 구분 불가였으므로 기각 근거가 아니다 |
| **interim·final의 `config`가 다름** | 로드 거절 — 순차 관측이 아니라 별개 실험이다(F9-b) |
| **interim 요건 충족 전에 final 요건이 먼저 충족** | 불가능(final 요건이 더 크다). 그래도 발생하면 둘 다 확정하고 이력에 이상으로 기록 |
| 하네스 익스포저와 라이브 익스포저 불일치 | 차단하지 않고 보드에 `하네스 100% / 라이브 {n}%`를 표시(Q5 확정 전 임시) |
| `preregistered.toml`의 `config`가 `engine.py` 기본값과 다름 | 정본 확정 **차단** + `사전등록 설정이 현재 엔진과 다르다`(F12) |

---

## 완료 기준

1. `store.run_harness()`가 인자 없이 호출되면 **`signalcfg.effective_config()`를 검사한다** — 테스트로 확인.
2. `sigdesk harness --preregistered <id>`가 등록 설정 그대로 돌고 `harness_runs`에 `is_locked` 판단과 함께 쌓인다.
3. `sigdesk harness --sweep`과 `mode="explore"`가 **`harness_last`를 건드리지 않는다** — 테스트로 확인.
4. `_verdict`가 `effective_periods`로 차단한다. 현 데이터로 PIT 정본을 돌리면
   `판정 불가 · 실효 리밸런스 표본 4회 < 최소 30회`가 나온다(요건 미달이 정직하게 드러난다).
5. `harness.py:418`의 `min_periods` PIT 완화가 **삭제됐다**.
6. `/api/proof`의 `A.harness.status`가 `pending`이고 `requirement`에 4/30·19/150·남은 거래일이 있다.
7. 관리자 실험 카드에서 가중치·선정 룰을 넣어 탐색 실행이 되고, 이력 표에 설정 해시가 보인다.
8. `signalcfg.set_dict()`·`reset()` 후 `signalcfg.history()`가 **비어 있지 않다**.
9. **`test_redteam.py`에 불변식 6개**가 추가되고 전체 테스트가 통과한다(현 809 passed 유지):
   - `test_harness_uses_effective_config` — 인자 없는 `run_harness`가 하드코딩 기본값을 쓰지 않는다
   - `test_explore_run_does_not_touch_board` — `mode=explore`·`--sweep` 후 `harness_last` 불변
   - `test_verdict_blocks_on_effective_periods` — empty가 대부분이면 판정 불가
   - `test_only_preregistered_can_lock` — `preregistered_id=null` 실행은 `is_locked=0`
   - `test_sidak_threshold_scales_with_registered_count` — n=2면 문턱 97.47%
   - `test_locked_verdict_invalidated_on_config_change` — `config_hash` 불일치 시 `invalidated`
   - `test_sidak_threshold_stable_after_interim_locks` — interim이 locked돼도 final 문턱이 97.47% 유지(F9-a 회귀 방지)
   - `test_interim_and_final_configs_must_match` — 두 look의 `config`·`harness`가 다르면 로드 거절(F9-b)
   - `test_preregistered_config_matches_engine_defaults` — `preregistered.toml`의 `config`가
     `SignalConfig` 기본값 + `kv` 오버라이드와 불일치면 확정 차단(F12)
12. `interim` look이 확정된 뒤 보드가 **final을 정본으로**, interim을 `중간 판독(참고)`으로 표시한다.
10. H1(technical 0.35→0)·H2(momentum=0)를 **소스 편집 없이** 탐색 실행으로 재현해 이력에 남긴다.
11. 기존 `harness_last.json`이 이력 첫 레코드로 흡수되고 `preregistered_id=null`이다.

---

## 결정 기록 (2026-08-05 인터뷰)

| # | 결정 |
|---|---|
| **D1** | 보드 정본 = **사전등록된 조합만**. 스윕·임의 실행은 이력에만 |
| **D2** | 실험 축 = **가중치 + 엔진 선정 룰**(`signalcfg.FIELDS` 전진) |
| **D3** | 판정 시점 = **사전에 정한 표본 요건 충족 시 1회**, 이후 동면 |
| **D4** | 사전등록 위치 = **git 커밋된 파일** `docs/preregistered.toml` |
| **D5** | 정본 대상 = **PIT 하네스만**. 가격 하네스는 탐색으로 강등해 보드에서 내림 |
| **D6** | 요건 = **interim(실효 12·PIT 60일) + final(실효 30·PIT 150일) 2 look 등록**. Šidák n=2 → 문턱 97.47%. interim 통과는 채택 근거가 아니다 |
| **D7** | `hold=5`로 등록하고 실측 지평(20거래일)과의 불일치를 `hypothesis`에 명시 (Q2 추천안) |
| **D8** | 진척은 **스냅샷 수만 세어 매일** 갱신(하네스 미실행). 하네스 실제 실행은 요건 90% 도달 후 매일 (Q3 추천안) |
| **D9** | N2의 거절 대상에 `brain_proposals.refresh()`의 **제안 생성도 포함**. 판정 전 제안은 만들지 않는다 (Q4 추천안) |
| **D10** | 하네스 익스포저는 **100% 유지**(`exposure=false`). 라이브(약세 국면 40%)와 다르다는 사실만 화면에 명시 (Q5-b) |
| **D11** | 사전등록 파일 형식 = **TOML**(`docs/preregistered.toml`). `tomllib` 표준 라이브러리 — 새 의존성 0 |

**D9 보충** — 생성까지 막는 쪽을 고른 이유: 생성을 허용하고 승인만 막으면 큐가 쌓이고,
쌓인 큐는 관리자 화면에 **배지**로 뜬다(`index.html:5441`). 판정 불가 상태에서 "5건 대기"가
매일 보이는 것은 승인을 유도하는 압력이다. 큐가 비는 편이 정직하다.
대신 `refresh()`가 거절될 때 **거절 이유를 화면에 남긴다** — 조용히 0건이면 고장과 구분이 안 된다.

---

## 미해결 질문

**Q6 — `min_pit_dates`를 "연속" 조건으로 볼 것인가.**
현 정의는 스냅샷이 있는 **날짜 수**다(연속 가정 없음). PIT에 구멍이 생기면 `effective_periods`가
자동으로 낮게 나오므로 이중으로 막을 필요는 없다고 판단했다. 다만 구멍이 특정 국면에 몰리면
표본이 편향된다 — **구멍 분포를 이력에 기록**하되 요건 조건에는 넣지 않는 것으로 시작한다.

---

## 참고 — 이 PRD가 고치는 진단 항목
개선 리스트 **#4**(Šidák·trials 통일) · **#11**(판정 이력 append-only) · **#14**(하네스 설정 주입) ·
로드맵 **N3**. 부분적으로 **#3**·**#10**(판정 게이트)의 선행 계약을 제공한다.
