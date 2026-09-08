"""적자 종목 프리미엄이 진짜 팩터인가, 이 구간(2026 상반기 +101% 버블)의 우연인가.

두 가지로 본다:
  ① 적자/흑자 라벨 자체의 h5 forward 수익 — 분모와 무관한 직접 검정
  ② 그 라벨의 **횡단면 IC** — 순위 정보가 있나
둘 다 **국면별로** 쪼갠다. 버블에서만 통하면 그건 팩터가 아니라 국면 베팅이다.
"""
import os, pathlib, sys, statistics, math
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pit6 import build
os.chdir(pathlib.Path(__file__).resolve().parents[2])  # 리포 루트
sys.path.insert(0, "src")
from signal_desk import signalcfg, store
from signal_desk.signals import harness as hz, pit_fundamentals as pf
from signal_desk.signals.accuracy import _spearman

H = 5
sc = signalcfg.get_config()
panel, scores, cov6, fired6, meta6, covers = build(sc)
hist = store.load_fundamentals_history()

# 그 날 알 수 있던 사업연도의 순이익 부호 → 적자 더미
def loss_dummy(date_str):
    fy = str(pf.latest_fiscal_year(date_str))
    out = {}
    for t, years in hist.items():
        m = (years or {}).get(fy)
        if not m: continue
        ni = m.get("net_income")
        if ni is None: continue
        out[t] = 1.0 if ni <= 0 else 0.0
    return out

idxs = [i for i in range(260, len(panel) - H - 1) if scores and any(
    (scores.get(t) or [None])[i] is not None for t in list(scores)[:50])]
idxs = idxs[::H]                      # 비중첩
regs = hz.regimes_at(panel, idxs)

by_reg = {}
ic_by_reg = {}
for i in idxs:
    d = panel.dates[i]
    dum = loss_dummy(d)
    rows = []
    for t, row in panel.closes.items():
        if (scores.get(t) or [None]*(i+1))[i] is None:  # 점수가 없는 날은 전략도 못 봄
            continue
        lab = dum.get(t)
        if lab is None: continue
        a, b = row[i + 1], row[i + 1 + H]
        if a and b and a > 0:
            rows.append((lab, b / a - 1.0))
    if len(rows) < 30: continue
    reg = regs.get(i) or "?"
    loss = [r for l, r in rows if l == 1.0]
    prof = [r for l, r in rows if l == 0.0]
    if len(loss) >= 3 and len(prof) >= 10:
        by_reg.setdefault(reg, []).append(
            (statistics.mean(loss) - statistics.mean(prof), len(loss), len(prof)))
    v = _spearman(rows, min_n=30)
    if v is not None:
        ic_by_reg.setdefault(reg, []).append(v)

def tstat(xs):
    n = len(xs)
    if n < 3: return None
    m = statistics.mean(xs); sd = statistics.stdev(xs)
    return m / (sd / math.sqrt(n)) if sd else None

print(f"기간 {panel.dates[idxs[0]]} ~ {panel.dates[idxs[-1]]} · 비중첩 관측 {len(idxs)}개 (h={H})\n")
print("① 적자 − 흑자 h5 평균 수익 차이 (국면별)")
print(f"   {'국면':<6} {'관측':>5} {'적자−흑자':>10} {'t':>7} {'평균 적자 종목수':>16}")
allx = []
for reg in sorted(by_reg, key=lambda r: -len(by_reg[r])):
    xs = [d for d, _, _ in by_reg[reg]]
    nl = statistics.mean([nl for _, nl, _ in by_reg[reg]])
    t = tstat(xs)
    allx += xs
    print(f"   {reg:<6} {len(xs):>5} {statistics.mean(xs)*100:>+9.2f}% "
          f"{(f'{t:+.2f}' if t is not None else '  n/a'):>7} {nl:>16.0f}")
t = tstat(allx)
print(f"   {'전체':<6} {len(allx):>5} {statistics.mean(allx)*100:>+9.2f}% {t:>+7.2f}")
print("\n② 적자 더미의 횡단면 IC (국면별) — 양수면 '적자일수록 오른다'")
print(f"   {'국면':<6} {'관측':>5} {'IC 평균':>9} {'t':>7}")
allic = []
for reg in sorted(ic_by_reg, key=lambda r: -len(ic_by_reg[r])):
    xs = ic_by_reg[reg]; allic += xs
    t = tstat(xs)
    print(f"   {reg:<6} {len(xs):>5} {statistics.mean(xs):>+9.4f} "
          f"{(f'{t:+.2f}' if t is not None else '  n/a'):>7}")
print(f"   {'전체':<6} {len(allic):>5} {statistics.mean(allic):>+9.4f} {tstat(allic):>+7.2f}")
