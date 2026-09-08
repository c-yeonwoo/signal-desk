# 측정 스크립트 (2026-09-06~07 세션)

하네스 노브로 흡수되지 **않은** 일회성 측정들. 결과는 모두
[`docs/harness-findings.md`](../../docs/harness-findings.md)에 기록돼 있고,
여기 있는 것은 **재현용**이다.

```bash
.venv/bin/python scripts/measure/<script>.py       # 리포 루트에서
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
