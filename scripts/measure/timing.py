"""국면 익스포저에 타이밍 능력이 있나 — 같은 평균 익스포저의 상수 전략과 비교한다.

하네스는 대조군에도 같은 익스포저를 걸므로 이 질문에 답하지 못한다(백분위 94.0 vs 94.5).
물어야 할 것은 "익스포저가 **미래 수익과 같은 방향으로** 움직였나"다.
"""
import os, pathlib, sys, statistics, math
os.chdir(pathlib.Path(__file__).resolve().parents[2])  # 리포 루트
sys.path.insert(0,"src")
import pandas as pd
from signal_desk.signals import regime

df = pd.read_parquet("data/cache/prices.parquet")
piv = df.pivot_table(index="date", columns="ticker", values="close").sort_index()
last = piv.loc["2026-08-14"].dropna().index
piv = piv[last]
ret = piv.pct_change(fill_method=None).mean(axis=1)      # 동일가중 시장 일간수익
dates = [d for d in piv.index if d >= "2024-01-01"]

rows=[]
for d in dates:
    hist = piv.loc[:d]
    closes = {t: hist[t].dropna().tolist() for t in last}
    r = regime.classify(closes)
    if not r["ready"]: continue
    rows.append((d, regime.target_exposure(r, None, None)["exposure"], r["regime"]))

ex = pd.Series({d:e for d,e,_ in rows})
fwd = ret.shift(-1).reindex(ex.index)                    # t 익스포저 → t+1 수익
ok = fwd.notna()
ex, fwd = ex[ok], fwd[ok]
n = len(ex)
mean_ex = ex.mean()

timed  = (ex * fwd).sum()          # 국면 익스포저 전략의 누적 수익(단순합 근사)
const  = (mean_ex * fwd).sum()     # 같은 평균 익스포저의 상수 전략
diff = (ex - mean_ex) * fwd        # 기간별 타이밍 기여
m, sd = diff.mean(), diff.std(ddof=1)
t = m / (sd / math.sqrt(n)) if sd else 0.0

print(f"구간 {ex.index[0]} ~ {ex.index[-1]} · {n}거래일")
print(f"평균 익스포저 {mean_ex*100:.1f}%  (분포: {ex.value_counts().sort_index().to_dict()})")
print()
print(f"국면 익스포저 누적    {timed*100:+.1f}%")
print(f"같은 평균 상수 익스포저 {const*100:+.1f}%")
print(f"타이밍 기여          {(timed-const)*100:+.1f}%p   t = {t:+.2f} (n={n})")
print()
print(f"corr(익스포저_t, 수익_t+1) = {ex.corr(fwd):+.4f}")
print()
# 국면별 그 다음날 평균 수익
g = pd.DataFrame({"regime":[r for _,_,r in rows]}, index=[d for d,_,_ in rows]).join(fwd.rename("fwd")).dropna()
print("국면별 익스포저 vs 그 다음날 시장 수익(평균):")
for reg_name, sub in g.groupby("regime"):
    e = regime.target_exposure({"regime":reg_name}, None, None)["exposure"]
    print(f"  {reg_name:4s} 익스포저 {e*100:3.0f}%  →  다음날 시장 {sub['fwd'].mean()*100:+.3f}%  (n={len(sub)})")
