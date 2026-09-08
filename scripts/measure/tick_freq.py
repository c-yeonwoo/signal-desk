"""표본 주기가 청산율을 얼마나 바꾸나 — 하네스(하루 1회) vs 30분틱(13회) vs 5분틱(78회).

장중 경로를 **브라운 브리지**로 만든다: 시가 O에서 종가 C로 가는 확산 경로.
확산 σ는 실측으로 분해했다 — 야간 갭 2.32%(분산 31%) · 장중 3.52%(71%).
경로가 없는 게 아니라 **표본이 없었을 뿐**이므로, 끝점을 고정한 브리지가 가장 보수적인 복원이다.
"""
import os, pathlib, sys
os.chdir(pathlib.Path(__file__).resolve().parents[2])  # 리포 루트
sys.path.insert(0, "src")
import numpy as np, pandas as pd
from signal_desk import strategy
from signal_desk.signals import risk, vol_sizing

SIG_INTRA = 0.0352          # 실측 장중 확산 σ(1일)
MAXH, STEP, SEED = 20, 10, 20260907
rng = np.random.default_rng(SEED)

df = pd.read_parquet("data/cache/prices.parquet")
d = df[df["date"] >= "2025-06-01"]
C = d.pivot_table(index="date", columns="ticker", values="close").sort_index()
O = d.pivot_table(index="date", columns="ticker", values="open").sort_index()
keep = C.iloc[-1].dropna().index
C, O = C[keep].values, O[keep].values
n_d, n_t = C.shape

def bridge(o, c, k):
    """log 공간 브리지 표본 k개(마지막이 종가). shape (k,)"""
    if k <= 1:
        return np.array([c])
    t = np.arange(1, k + 1) / k
    dw = rng.normal(0.0, np.sqrt(1.0 / k), k)
    w = np.cumsum(dw)
    br = w - t * w[-1]                              # 브라운 브리지(양 끝 0)
    lo, lc = np.log(o), np.log(c)
    return np.exp(lo + (lc - lo) * t + SIG_INTRA * br)

USE_OPEN = os.environ.get("USE_OPEN", "1") == "1"


def walk(j, e_i, k, rules):
    entry = C[e_i, j]
    peak = entry
    end = min(e_i + MAXH, n_d - 1)
    for i in range(e_i + 1, end + 1):
        o, c = (O[i, j] if USE_OPEN else C[i - 1, j]), C[i, j]
        if not (np.isfinite(o) and np.isfinite(c) and o > 0 and c > 0):
            continue
        # USE_OPEN=1: 갭은 시가에서 한 번 점프(현실). 0: 갭을 경로로 번지게 함(상한 추정).
        for px in (np.concatenate(([o], bridge(o, c, k))) if k > 1 else [c]):
            peak = max(peak, px)
            if risk.check_exit(entry, px, peak, rules):
                return px / entry - 1.0, i - e_i
    last = C[end, j]
    return (last / entry - 1.0, MAXH) if np.isfinite(last) else (0.0, MAXH)

print(f"장중 확산 σ {SIG_INTRA*100:.2f}%/일 · 보유상한 {MAXH}일 · 진입 {STEP}일 간격 · seed {SEED}\n")
print(f"{'표본/일':>8} {'해당 주기':<12} {'조기청산율':>10} {'평균 보유':>9} {'평균 수익':>9} {'중위':>8}")
print("-" * 66)
print("USE_OPEN =", USE_OPEN, "(1=갭 점프·현실 · 0=갭을 경로로 번짐·상한)\n")
for k, label in ((1, "하네스(종가)"), (78, "5분틱(매도)")):
    rets, days, early = [], [], 0
    for j in range(n_t):
        vol = None
        for e_i in range(140, n_d - MAXH - 1, STEP):
            if not (np.isfinite(C[e_i, j]) and C[e_i, j] > 0):
                continue
            col = C[max(0, e_i - 25):e_i + 1, j]
            col = col[np.isfinite(col)]
            vol = vol_sizing.realized_vol(list(col)) if len(col) > 21 else None
            rules = strategy.risk_config("balanced", "약세", sigma=vol)
            r, hd = walk(j, e_i, k, rules)
            rets.append(r); days.append(hd)
            if hd < MAXH: early += 1
    rets, days = np.array(rets), np.array(days)
    print(f"{k:>8} {label:<12} {early/len(rets)*100:>9.1f}% {days.mean():>8.1f}일 "
          f"{rets.mean()*100:>+8.2f}% {np.median(rets)*100:>+7.2f}%")
