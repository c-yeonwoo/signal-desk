# 측정 스크립트 (2026-09-06~07 세션)

하네스 노브로 흡수되지 **않은** 일회성 측정들. 결과는 모두
[`docs/harness-findings.md`](../../docs/harness-findings.md)에 기록돼 있고,
여기 있는 것은 **재현용**이다.

```bash
.venv/bin/python scripts/measure/<script>.py       # 리포 루트에서
```

## 2026-10-11 일봉 판단 재생 기반

`.venv/bin/python -m scripts.measure.research_daily_foundation --window 2026q2early-v5 --horizon 5`는 보존된 KRX 원문과 가격 전용 판단 기록을 연구용 판단·현금 재생에 연결한다. `--window`는 이미 열람한 2024년 세 구간과 2025~2026년 네 구간, `--horizon`은 5·20만 받는다. 각 조합에서 기존 `holding_horizon_portfolio_lab`의 체결 순서·가격·수량과 비용 후 결과가 일치하는지 `legacy_replay_parity`로 보고한다. 당시 순위와 공급자 도착 시각이 없는 입력은 가정으로 표시한다. 이 보고서는 등록 판정·운영 주문·새 전략 수익 증거가 아니다. 세부 계약과 남은 조건은 [엔진 재설계](../../docs/ENGINE-REBUILD-BLUEPRINT-2026-10-11.md)에 있다.

`--sensitivity`를 더하면 같은 개발 입력에 비용 25/75/120bp, 불리한 시가 체결, 부분체결·미체결 시나리오를 적용한다. 손익이 없거나 더 좋아지는 시나리오도 숨기지 않는다. 이는 **가격만 사용한 가상 계산**이며 KRX 일별 원본은 기업행사·배당의 완전한 원천이 아니다. 검증된 총수익, 독립 표본 성과, 실제 주문 체결로 읽지 않는다.

### 과거 기업행사 원문 보존·검수

`acquire_historical_sources.py`의 `dart-actions`는 한 발행사의 최대 32일 접수 목록과 모든 원문 ZIP을, `kis-dividend`/`kis-rev-split`은 한 종목의 최대 32일 과거 일정 응답을 요청 상한 내에서 읽기 전용으로 보존한다. 새 ZIP을 `data/research/`에 저장하며, 기존 파일을 덮어쓰지 않는다. `--end`는 등록 보호 시작 전이어야 한다. 실제 키는 로컬 환경 설정에서 읽고 출력하지 않는다.

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

## 왜 `pit6.py` 가 있나 (먼저 읽을 것)

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
