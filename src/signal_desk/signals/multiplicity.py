"""다중검정 보정 — 몇 번 시도했는지를 세고, 그만큼 문턱을 올린다.

## 왜 필요한가

이 리포의 핵심 실패 모드는 **고르기**다. `CLAUDE.md`가 이미 적어 뒀다 —
*"8개 조합을 한 번에 보면 판별력이 전혀 없어도 그중 하나가 95%를 넘을 확률이 34%다.
스윕 결과에서 초록 한 칸을 골라 쓰는 건 측정이 아니라 고르기다."*

사전등록(`prereg`)은 **정본 look 수**로 Šidák 보정을 한다. 그건 "같은 가설을 몇 번 볼 것인가"를
막는다. 하지만 그 앞단에 **탐색으로 몇 개 조합을 돌려봤는가**가 있고, 그건 보정되지 않았다.
`db.harness_runs`가 append-only로 전부 남아 있으므로 **세면 된다**(L4).

그 수를 쓰는 곳이 Deflated Sharpe Ratio(L3)다 — Bailey & López de Prado(2014).
관측된 Sharpe가 "N번 시도했을 때 우연히 나올 수 있는 최대 Sharpe"보다 큰지를 묻는다.

## 무엇을 하지 않는가

- **Sharpe 절대값을 판정에 쓰지 않는다.** 이 리포의 판정은 라벨 치환 대조군 대비 백분위다.
  DSR은 그 판정을 **대체하지 않고** 보조한다 — "시도 횟수를 고려해도 남는가"라는 별개 질문이다.
- **DSR이 통과했다고 판별력이 있다고 말하지 않는다.** DSR은 Sharpe 하나에 대한 검정이고,
  Sharpe는 생존편향·거래비용 가정을 그대로 물려받는다.
"""

from __future__ import annotations

import math

# Euler–Mascheroni 상수 — 극단값 기대치(E[max])의 1차 근사에 들어간다.
_EULER = 0.5772156649015329


def norm_cdf(z: float) -> float:
    """표준정규 CDF. `math.erf`만 쓴다."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """표준정규 분위수(역 CDF) — Acklam 유리근사(|오차| < 1.15e-9).

    scipy 없이 E[max SR]을 계산하려면 이게 필요하다. 경계(p<=0, p>=1)는 유한값으로 클램프한다 —
    inf를 돌려주면 그 뒤 산술이 전부 nan이 되고, nan은 화면에서 "값 없음"과 구분되지 않는다.
    """
    if p <= 0.0:
        return -8.5
    if p >= 1.0:
        return 8.5
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def moments(rets: list[float]) -> dict:
    """기간 수익률의 평균·표준편차·왜도·첨도(비초과). DSR이 셋 다 쓴다.

    왜도·첨도를 빼고 Sharpe만 보면 **비대칭·꼬리 위험을 공짜로 얻은 것처럼** 보인다 —
    옵션 매도처럼 평소엔 잘 맞고 한 번에 크게 잃는 전략이 높은 Sharpe를 받는다.
    """
    n = len(rets)
    if n < 2:
        return {"n": n, "mean": None, "sd": None, "skew": None, "kurtosis": None}
    mu = sum(rets) / n
    m2 = sum((r - mu) ** 2 for r in rets) / n
    sd = math.sqrt(m2)
    if sd <= 0:
        return {"n": n, "mean": mu, "sd": 0.0, "skew": None, "kurtosis": None}
    m3 = sum((r - mu) ** 3 for r in rets) / n
    m4 = sum((r - mu) ** 4 for r in rets) / n
    return {"n": n, "mean": mu, "sd": sd,
            "skew": m3 / sd ** 3, "kurtosis": m4 / sd ** 4}


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """N번 독립 시도했을 때 기대되는 **최대** Sharpe(귀무: 참 Sharpe 0).

    Bailey & López de Prado (2014) 식 (5):
        E[max SR] ≈ sqrt(V[SR]) · [ (1-γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ]

    N=1이면 0이다 — 한 번만 시도했으면 고르기가 없다.
    """
    n = max(1, int(n_trials))
    if n == 1 or sr_variance <= 0:
        return 0.0
    return math.sqrt(sr_variance) * (
        (1 - _EULER) * norm_ppf(1 - 1.0 / n) + _EULER * norm_ppf(1 - 1.0 / (n * math.e)))


def deflated_sharpe(rets: list[float], *, n_trials: int,
                    sr_variance: float | None = None,
                    periods_per_year: float | None = None) -> dict:
    """Deflated Sharpe Ratio — "N번 시도한 걸 감안해도 이 Sharpe가 남는가".

    `rets`는 **기간(리밸런스) 수익률**이다. Sharpe도 기간 단위로 계산하고 연율화하지 않는다 —
    연율화하면 √(기간/년)이 곱해져 검정통계량이 부풀려진다(DSR은 비연율 SR을 요구한다).
    `periods_per_year`를 주면 **표시용** 연율 Sharpe를 함께 낸다(판정에는 쓰지 않는다).

    `sr_variance`(시도들 간 Sharpe 분산)를 주면 그것을 쓰고, 없으면 이론값 1/(T−1)로 대체하고
    `sr_variance_source`에 어느 쪽인지 남긴다 — 근사를 실측처럼 보이게 두지 않는다.
    """
    m = moments(rets)
    out: dict = {
        "n_trials": max(1, int(n_trials)), "n_periods": m["n"],
        "sharpe": None, "sharpe_annualized": None,
        "skew": (round(m["skew"], 3) if m["skew"] is not None else None),
        "kurtosis": (round(m["kurtosis"], 3) if m["kurtosis"] is not None else None),
        "expected_max_sharpe": None, "sr_variance": None, "sr_variance_source": None,
        "dsr": None, "significant": False, "blocked_reason": None,
    }
    t = m["n"]
    if t is None or t < 4:
        out["blocked_reason"] = f"기간 표본 {t or 0}개 — DSR은 왜도·첨도가 필요해 최소 4개"
        return out
    if not m["sd"]:
        out["blocked_reason"] = "기간 수익률의 표준편차가 0 — Sharpe를 정의할 수 없다"
        return out
    sr = m["mean"] / m["sd"]
    out["sharpe"] = round(sr, 4)
    if periods_per_year:
        out["sharpe_annualized"] = round(sr * math.sqrt(periods_per_year), 2)
    if sr_variance is not None and sr_variance > 0:
        var, src = float(sr_variance), "measured"
    else:
        var, src = 1.0 / (t - 1), "theoretical_1_over_T"
    out["sr_variance"] = round(var, 6)
    out["sr_variance_source"] = src
    sr0 = expected_max_sharpe(out["n_trials"], var)
    out["expected_max_sharpe"] = round(sr0, 4)
    # 분모: Sharpe 추정량의 표준오차(왜도·첨도 보정). 음수면 정의 불가.
    denom_sq = 1.0 - m["skew"] * sr + (m["kurtosis"] - 1.0) / 4.0 * sr ** 2
    if denom_sq <= 0:
        out["blocked_reason"] = ("왜도·첨도 보정 분모가 0 이하 — 꼬리가 너무 두꺼워 "
                                 "이 표본에서 DSR을 정의할 수 없다")
        return out
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom_sq)
    out["dsr"] = round(norm_cdf(z), 4)
    out["significant"] = bool(out["dsr"] >= 0.95)
    if not out["significant"]:
        out["blocked_reason"] = (
            f"시도 {out['n_trials']}회를 감안한 기대 최대 Sharpe {sr0:+.3f} 대비 "
            f"관측 {sr:+.3f} — DSR {out['dsr']:.3f} < 0.95")
    return out


# ── Hansen SPA (L2) ───────────────────────────────────────────────────────────
# 라벨 치환 대조군(`harness._null_distribution`)은 **한 전략**이 그 시뮬레이터의 기계적 조건
# 아래에서 정보를 갖는지 검정한다. SPA가 답하는 것은 다른 질문이다 — **여러 조합을 돌려보고
# 그중 최고를 골랐을 때**, 그 최고가 벤치마크보다 낫다고 말할 수 있는가.
#
# 이 리포는 실제로 그 함정을 문서화했다: "8개 조합을 한 번에 보면 판별력이 전혀 없어도 그중
# 하나가 95%를 넘을 확률이 34%다." Šidák은 문턱을 올려 그걸 막지만, 조합들이 **서로 상관**되어
# 있을 때(대개 그렇다) 과도하게 보수적이다. SPA는 부트스트랩으로 그 상관을 그대로 반영한다.


def _stationary_bootstrap_indices(n: int, avg_block: float, rng) -> list[int]:
    """Politis–Romano 정상 부트스트랩 인덱스.

    블록 길이를 기하분포로 뽑아 **자기상관을 보존**한다. iid 부트스트랩을 쓰면 기간 수익률의
    자기상관(중첩 창·모멘텀 지속성)이 사라져 p가 작게 나온다 — Newey-West를 쓰는 이유와 같다.
    """
    p = 1.0 / max(1.0, avg_block)
    idx = [rng.randrange(n)]
    for _ in range(n - 1):
        idx.append(rng.randrange(n) if rng.random() < p else (idx[-1] + 1) % n)
    return idx


def spa_test(diffs_by_model: dict[str, list[float]], *, trials: int = 1000,
             avg_block: float = 5.0, seed: int = 20260806) -> dict:
    """Hansen(2005) SPA — "가장 좋은 조합이 벤치마크보다 낫다"의 p-value.

    `diffs_by_model`: {조합이름: [기간별 (전략수익 − 벤치마크수익)]}. 모든 조합의 길이가 같아야
    한다(같은 날짜축에서 돌린 것이어야 비교가 성립한다).

    검정통계량은 studentized 최댓값 `max_k √T·mean(d_k)/sd(d_k)`이고, 귀무(모든 조합이
    벤치마크보다 낫지 않다) 아래 분포를 정상 부트스트랩으로 만든다. Hansen의 재중심화
    (recentering)를 쓴다 — 성적이 나쁜 조합이 귀무 분포를 끌어올려 p를 부풀리는 것을 막는다.

    반환의 `best`는 통계량이 가장 큰 조합이다. **그 조합을 채택하라는 뜻이 아니다** —
    p가 작아도 "고른 것"이라는 사실은 남으므로 사전등록 없이 쓰면 여전히 사후선택이다.
    """
    import random as _random

    names = [k for k, v in (diffs_by_model or {}).items() if v]
    out: dict = {"n_models": len(names), "n_periods": 0, "trials": int(trials),
                 "avg_block": avg_block, "statistic": None, "p_value": None,
                 "best": None, "significant": False, "blocked_reason": None}
    if not names:
        out["blocked_reason"] = "비교할 조합이 없다"
        return out
    lens = {len(diffs_by_model[k]) for k in names}
    if len(lens) != 1:
        out["blocked_reason"] = f"조합별 기간 수가 다르다({sorted(lens)}) — 같은 날짜축이 아니다"
        return out
    t = lens.pop()
    out["n_periods"] = t
    if t < 8:
        out["blocked_reason"] = f"기간 표본 {t}개 — 부트스트랩에 최소 8개"
        return out

    stats: dict[str, float] = {}
    mus: dict[str, float] = {}
    sds: dict[str, float] = {}
    for k in names:
        d = diffs_by_model[k]
        mu = sum(d) / t
        var = sum((x - mu) ** 2 for x in d) / t
        sd = math.sqrt(var)
        mus[k], sds[k] = mu, sd
        stats[k] = (math.sqrt(t) * mu / sd) if sd > 0 else 0.0
    best = max(names, key=lambda k: stats[k])
    observed = max(0.0, stats[best])
    out["statistic"] = round(observed, 4)
    out["best"] = best
    out["per_model"] = {k: {"mean_diff": round(mus[k], 6), "t": round(stats[k], 3)} for k in names}

    # 재중심화 문턱 — 성적이 이 아래인 조합은 귀무 분포에서 0으로 눌러 p 부풀림을 막는다.
    thresh = {k: -sds[k] * math.sqrt(2.0 * math.log(math.log(t)) / t) if t > 3 else 0.0
              for k in names}
    rng = _random.Random(seed)
    ge = 0
    for _ in range(int(trials)):
        idx = _stationary_bootstrap_indices(t, avg_block, rng)
        boot = 0.0
        for k in names:
            d = diffs_by_model[k]
            bmu = sum(d[i] for i in idx) / t
            centre = mus[k] if mus[k] >= thresh[k] else 0.0
            z = (math.sqrt(t) * (bmu - centre) / sds[k]) if sds[k] > 0 else 0.0
            boot = max(boot, z)
        if boot >= observed:
            ge += 1
    out["p_value"] = round((ge + 1) / (int(trials) + 1), 4)   # +1 보정(p=0을 만들지 않는다)
    out["significant"] = bool(out["p_value"] < 0.05)
    if not out["significant"]:
        out["blocked_reason"] = (
            f"조합 {len(names)}개 중 최고({best})도 벤치마크 대비 p={out['p_value']:.3f} — "
            f"고르기를 감안하면 우위라 말할 수 없다")
    return out
