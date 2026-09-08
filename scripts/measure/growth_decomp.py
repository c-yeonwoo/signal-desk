"""성장 가중 arm을 픽 단위로 분해 — 새 규칙 소급 적용."""
import os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pit6 import build
os.chdir(pathlib.Path(__file__).resolve().parents[2])  # 리포 루트
sys.path.insert(0, "src")
from signal_desk import signalcfg
from signal_desk.signals import harness as hz
sc = signalcfg.get_config()

panel, s0, cov, fired, _m, covers = build(sc, growth_weight=0.0)
cfg = hz.HarnessConfig(top_pct=3.0, rebalance_days=5, cost_pct=0.25, signal_config=sc)
for w in (0.15, 0.30, 0.45):
    _p, sw, *_ = build(sc, growth_weight=w)
    o = hz.compare_picks(panel, s0, sw, cfg)
    a, b, c = o["only_a"], o["only_b"], o["common"]
    print(f"성장 가중 0.00 vs {w:.2f}  ({o['periods']}기간)")
    print(f"   교체 {o['n_swapped']}건 / 공통 {c['n']}건  → 교체 비율 "
          f"{o['n_swapped']/max(1,o['n_swapped']+c['n'])*100:.1f}%")
    print(f"   0.00만 {a['n']:>3}건 {a['mean_pct']:>+7}% (sd {a['sd_pct']})  |  "
          f"{w:.2f}만 {b['n']:>3}건 {b['mean_pct']:>+7}% (sd {b['sd_pct']})")
    print(f"   교체 손익 {o['diff_pp']:+}%p ± {o['diff_se_pp']}%p  →  **Welch t = {o['t']}**\n")
