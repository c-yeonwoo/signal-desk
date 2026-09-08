"""재정규화 편향을 직접 잰다 — 커버리지별 |점수| 평균. 고치면 이 기울기가 사라져야 한다."""
import os, pathlib, sys, statistics
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pit6 import build
os.chdir(pathlib.Path(__file__).resolve().parents[2])  # 리포 루트
sys.path.insert(0, "src")
from signal_desk import signalcfg
sc = signalcfg.get_config()

for label, fd in (("현행 (발동 가중 분모)", False), ("결측=중립 0 (countable 분모)", True)):
    panel, scores, cov6, fired6, meta6, covers = build(sc, full_denominator=fd)
    buckets = {}
    allsc = []
    for t, row in scores.items():
        cv = covers.get(t) or []
        for i, v in enumerate(row):
            c = cv[i] if i < len(cv) else None
            if v is None or c is None:
                continue
            k = round(c, 2)
            buckets.setdefault(k, []).append(abs(v))
            allsc.append(v)
    print(f"\n■ {label}   (종목·날짜 {len(allsc):,}셀)")
    print(f"   {'커버리지':>8} {'셀 수':>8} {'|점수| 평균':>11}")
    for k in sorted(buckets):
        v = buckets[k]
        if len(v) < 200:
            continue
        print(f"   {k:>8.2f} {len(v):>8,} {statistics.mean(v):>11.3f}")
    allsc.sort()
    n = len(allsc)
    q = lambda p: allsc[min(n-1, int(n*p))]
    print(f"   점수 분포: p50 {q(0.5):+.2f} · p90 {q(0.9):+.2f} · p97 {q(0.97):+.2f} · max {allsc[-1]:+.2f}")
    above = sum(1 for v in allsc if v >= 1.2)
    print(f"   min_score 1.2 이상: {above:,}셀 ({above/n*100:.1f}%)")
