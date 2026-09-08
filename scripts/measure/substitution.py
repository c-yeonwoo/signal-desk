"""분모를 바꾸면 **누가 누구로 교체되나**, 그리고 그 교체가 수익에 어떻게 작용했나.

13.7pp를 만든 것이 '적자 프리미엄'이 아니라면 무엇인가 — 교체된 픽을 직접 센다.
"""
import os, pathlib, sys, statistics, math
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pit6 import build
os.chdir(pathlib.Path(__file__).resolve().parents[2])  # 리포 루트
sys.path.insert(0, "src")
from signal_desk import signalcfg
sc = signalcfg.get_config()
H, K, MINSC = 5, 6, 1.2

pA = build(sc, full_denominator=False)
pB = build(sc, full_denominator=True)
panel, sA, covers = pA[0], pA[1], pA[5]
sB = pB[1]

def picks(scores, i):
    rows = [(scores[t][i], t) for t in scores
            if i < len(scores[t]) and scores[t][i] is not None]
    if len(rows) < 30: return None
    rows.sort(reverse=True)
    return [t for v, t in rows[:K] if v >= MINSC]

def fwd(t, i):
    row = panel.closes[t]
    a, b = row[i + 1], row[i + 1 + H]
    return (b / a - 1.0) if (a and b and a > 0) else None

only_a, only_b, both = [], [], []
a_cov, n_per = [], 0
for i in range(260, len(panel) - H - 1, H):
    pa, pb = picks(sA, i), picks(sB, i)
    if pa is None or pb is None: continue
    n_per += 1
    sa, sb = set(pa), set(pb)
    for t in sa - sb:
        r = fwd(t, i)
        if r is not None:
            only_a.append(r)
            c = (covers.get(t) or [None]*(i+1))[i]
            a_cov.append(c if c is not None else 1.0)
    for t in sb - sa:
        r = fwd(t, i)
        if r is not None: only_b.append(r)
    for t in sa & sb:
        r = fwd(t, i)
        if r is not None: both.append(r)

def line(lbl, xs):
    if not xs: return f"   {lbl:<22} 0건"
    n = len(xs); m = statistics.mean(xs)
    sd = statistics.stdev(xs) if n > 1 else 0
    t = m/(sd/math.sqrt(n)) if sd else 0
    return (f"   {lbl:<22} {n:>5}건  평균 {m*100:>+7.2f}%  중위 {statistics.median(xs)*100:>+7.2f}%"
            f"  sd {sd*100:>5.2f}%  t {t:>+5.2f}")

print(f"기간 {n_per}기간 (h={H} 비중첩)\n")
print("분모를 바꿨을 때 매수권 교체 — h5 실현수익")
print(line("A만 (현행이 산 것)", only_a))
print(line("B만 (결측중립이 산 것)", only_b))
print(line("공통", both))
if only_a and only_b:
    d = statistics.mean(only_a) - statistics.mean(only_b)
    print(f"\n   교체 손익: A만 − B만 = {d*100:+.2f}%p / 픽")
if a_cov:
    print(f"   A만 픽의 커버리지: 평균 {statistics.mean(a_cov):.3f} · "
          f"1.0 미만 {sum(1 for c in a_cov if c < 1.0)}/{len(a_cov)}건")
