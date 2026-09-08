"""분모에 따라 매수권(상위 6자리)의 구성이 어떻게 달라지나 — 커버리지 분포로 본다."""
import os, pathlib, sys, statistics
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pit6 import build
os.chdir(pathlib.Path(__file__).resolve().parents[2])  # 리포 루트
sys.path.insert(0, "src")
from signal_desk import signalcfg
sc = signalcfg.get_config()
K, MINSC = 6, 1.2

for label, fd in (("현행(발동 가중)", False), ("결측=중립 0", True)):
    panel, scores, cov6, fired6, meta6, covers = build(sc, full_denominator=fd)
    n = len(panel.dates)
    tot = part = 0
    cov_of_picks = []
    for i in range(260, n):
        rows = []
        for t, row in scores.items():
            v = row[i] if i < len(row) else None
            if v is None: continue
            c = (covers.get(t) or [None]*(i+1))[i]
            rows.append((v, t, c))
        if len(rows) < 30: continue
        rows.sort(reverse=True)
        for v, t, c in rows[:K]:
            if v < MINSC: continue
            tot += 1
            cov_of_picks.append(c if c is not None else 1.0)
            if c is not None and c < 1.0: part += 1
    print(f"■ {label}: 창 진입 {tot:,}건 · 그중 커버리지<1.0 이 {part:,}건 ({part/max(1,tot)*100:.1f}%) "
          f"· 평균 커버리지 {statistics.mean(cov_of_picks):.3f}")
