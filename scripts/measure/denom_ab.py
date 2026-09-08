"""분모 A/B — 점수·유니버스·비용·게이트 전부 동일, 분모만 다르다. 시드 5개."""
import os, pathlib, sys, statistics
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pit6 import build
os.chdir(pathlib.Path(__file__).resolve().parents[2])  # 리포 루트
sys.path.insert(0, "src")
from signal_desk import signalcfg
from signal_desk.signals import harness as hz

TRIALS = int(os.environ.get("TRIALS", "200"))
SEEDS = [20260726, 7, 4242, 99991, 31337]
sc = signalcfg.get_config()
print(f"trials={TRIALS} · 시드 {len(SEEDS)}개", flush=True)
print(f"{'분모':<26} | {'백분위(시드별)':<40} {'평균':>7} {'sd':>7} | {'실효기간':>7} {'보유':>5} {'승률':>6}", flush=True)
print("-"*112, flush=True)
for label, fd in (("현행(발동 가중)", False), ("결측=중립 0(countable)", True)):
    panel, scores, cov6, fired6, meta6, covers = build(sc, full_denominator=fd)
    ps, meta = [], None
    for sd in SEEDS:
        cfg = hz.HarnessConfig(top_pct=3.0, rebalance_days=5, cost_pct=0.25,
                               random_trials=TRIALS, signal_config=sc, seed=sd)
        out = hz.run(panel, cfg, None, scores=scores, score_source="price6",
                     coverage=cov6, fired=fired6, covers=covers)
        ps.append(out["vs_random"]["percentile"])
        meta = out
    st = meta["strategy"]
    print(f"{label:<26} | {str([round(p,1) for p in ps]):<40} {statistics.mean(ps):>6.1f}% "
          f"{statistics.stdev(ps):>6.1f}pp | {meta.get('effective_periods'):>7} "
          f"{st['avg_picks']:>5} {st['win_rate_pct']:>5}%", flush=True)
    print(f"{'':<26} |   커버리지 게이트 차단 {meta.get('data_coverage_gate',{}).get('blocked')}회 · "
          f"전략 누적 {st['total_ret_pct']}% · 위상편차 {st['phase_spread_pp']}pp", flush=True)
