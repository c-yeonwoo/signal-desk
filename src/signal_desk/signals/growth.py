"""성장 팩터 — 매출성장의 **횡단면 분위**. 지금은 하네스 전용(라이브 가중 없음).

## 왜 별도 팩터를 재보나

매출성장은 이미 `fundamental` 안에 있다 — 다만 **±0.7짜리 계단 하나**로, 총 ±2.0 중
일부다(`fundamental.score`: >15% +0.7 · 5~15% +0.3 · <0% −0.5). 그래서 성장은 자기
가중치를 갖지 못하고 PER·PBR·ROE와 한 덩어리로 묶여 있다.

미국 빅테크가 매수권에 못 들어오는 구조가 이걸 드러냈다(2026-09-06): `fundamental` 은
PER>25에 −0.5, PBR>3에 −0.3을 절대 기준으로 물리는데 성장은 그 감점을 상쇄할 만큼의
가중을 갖지 못한다. "비싼 데는 이유가 있다"가 점수에 들어갈 자리가 없는 것이다.

## 왜 절대 임계가 아니라 분위인가

`fundamental` 의 계단은 절대값(15%·5%·0%)이다. 절대 문턱은 분포가 이동하면 뜻이 바뀌고,
이 리포는 그걸로 이미 두 번 데였다(매수문턱이 관측 최고점수보다 높았던 것, 트레일링 폭이
시장마다 다른 σ였던 것). 그래서 여기서는 **그 날 후보 집합 안의 상대 순위**로 낸다 —
`valuation` 과 같은 규약이고, 극단 성장률(실측 최대 +1875%)이 혼자 분포를 먹는 것도 막는다.

## 이 모듈이 답하지 않는 것

- **미국에서는 잴 수 없다.** `us_fundamentals.json` 은 현재값 스냅샷 하나뿐이라
  매출성장 이력이 없다(국내는 `fundamentals_history.json` 에 3년치 · 셀 커버리지 91.5%).
  즉 여기서 나오는 판별력은 **국내 결과**이고, 미국으로 옮겨지는지는 별개 질문이다.
- 섹터 중립화를 하지 않는다. 성장률도 섹터마다 다르지만, 국내 섹터 맵은 큐레이션 200종목이고
  PIT 유니버스는 305종목이라 커버리지가 갈린다. 분위를 섹터로 자를지는 다음 실험이다.
"""

from __future__ import annotations

# 순위 규약을 **한 곳에서만** 정한다 — 두 팩터가 다른 랭킹을 쓰면 분위의 뜻이 갈린다.
from signal_desk.signals.valuation import _percentile_rank

METRIC = "revenue_growth"


def percentile_scores(metrics_by_ticker: dict[str, dict]) -> dict[str, float]:
    """ticker -> 성장 percentile(0=최저 성장, 100=최고 성장). 값 없는 종목은 아예 넣지 않는다.

    `valuation` 과 같은 `_percentile_rank`(동순위=평균 랭크)를 쓴다.
    """
    vals = {t: m[METRIC] for t, m in (metrics_by_ticker or {}).items()
            if isinstance(m, dict) and m.get(METRIC) is not None}
    return _percentile_rank(vals) if vals else {}


def component(ticker: str, pct_scores: dict[str, float], weight: float
              ) -> tuple[float, float, list[str]]:
    """percentile → [-1,1]. **높은 성장이 +1** (저평가와 부호 방향이 반대라 명시해 둔다).

    percentile 자체가 없는 종목은 가중치 0으로 완전히 제외한다 — 0점으로 넣으면 '성장을
    모른다'가 '성장이 중간이다'로 번역된다.
    """
    pct = (pct_scores or {}).get(ticker)
    if pct is None or not weight:
        return 0.0, 0.0, []
    norm = (pct - 50) / 50
    zone = "고성장" if pct >= 50 else "저성장"
    # 분위를 그대로 쓴다("상위 N%"로 뒤집으면 최상위가 '상위 0%'로 읽힌다).
    return norm, float(weight), [f"[성장] 매출성장 분위 {pct:.0f}/100 — {zone} 구간"]
