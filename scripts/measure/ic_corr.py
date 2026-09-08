"""국내·미국 횡단면 IC의 **날짜별 상관**. 두 시장을 합쳤을 때 표본이 실제로 얼마나 빨리 차는가.

주의(대리 측정): 라이브 점수는 국내 8팩터·미국 6팩터인데 여기서는 두 시장 모두
**가격 기반 점수**(기술·낙폭과대·모멘텀 — `harness._score_series`)를 쓴다. 미국은 PIT 재무
이력이 없어 그 방법밖에 없고, 국내도 같은 기준으로 맞춰야 비교가 성립한다.
그래서 이 ρ는 '두 시장의 팩터 IC가 같은 날 얼마나 같이 움직이나'의 근사다.
"""
import os, pathlib, sys, statistics, math
os.chdir(pathlib.Path(__file__).resolve().parents[2])  # 리포 루트
sys.path.insert(0, "src")
import pandas as pd
from signal_desk.signals import harness as hz
from signal_desk.signals.accuracy import _spearman
from signal_desk.signals.engine import SignalConfig

H = 5
cfg = SignalConfig()

def ic_series(parquet, label):
    df = pd.read_parquet(parquet)
    dated = {}
    for t, g in df.groupby("ticker"):
        g = g.sort_values("date")
        dated[t] = (g["date"].astype(str).tolist(), g["close"].astype(float).tolist())
    panel = hz.build_panel(dated)
    scores, can, fired, _cov = hz._score_series(panel, cfg)
    print(f"  {label}: {len(panel)}일 · 점수 {len(scores)}종목 · 발동률 {fired}")
    out = {}
    n = len(panel.dates)
    for i in range(n - H - 1):
        pairs = []
        for t, row in panel.closes.items():
            sc = scores.get(t)
            if not sc or sc[i] is None:
                continue
            a, b = row[i + 1], row[i + 1 + H]
            if a and b and a > 0:
                pairs.append((sc[i], b / a - 1.0))
        if len(pairs) >= 30:
            v = _spearman(pairs, min_n=30)
            if v is not None:
                out[panel.dates[i]] = v
    return out

print("점수·IC 시계열 만드는 중…")
kr = ic_series("data/cache/prices.parquet", "국내")
us = ic_series("data/cache/us_prices.parquet", "미국")

common = sorted(set(kr) & set(us))
print(f"\n공통 거래일 {len(common)}일 ({common[0]} ~ {common[-1]})")

# h=5 비중첩 블록만 — 중첩 창은 자기상관이 있어 상관 추정도 부풀린다
nonov = common[::H]
a = [kr[d] for d in nonov]
b = [us[d] for d in nonov]
n = len(a)
ma, mb = statistics.mean(a), statistics.mean(b)
sa, sb = statistics.pstdev(a), statistics.pstdev(b)
rho = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n * sa * sb)
se = 1 / math.sqrt(max(1, n - 3))          # Fisher z 근사
lo, hi = math.tanh(math.atanh(rho) - 1.96 * se), math.tanh(math.atanh(rho) + 1.96 * se)

print(f"비중첩 관측 {n}쌍 (h={H})")
print(f"  국내 IC 평균 {ma:+.4f} · sd {sa:.4f}")
print(f"  미국 IC 평균 {mb:+.4f} · sd {sb:.4f}")
print(f"  ρ(국내 IC, 미국 IC) = {rho:+.3f}   95% CI [{lo:+.3f}, {hi:+.3f}]")

print("\n=== 두 시장을 합쳤을 때 필요한 거래일 ===")
NEED, KR_ONLY_TD = 63, 63 * H
print(f"국내 단독: {KR_ONLY_TD}거래일 (독립관측 1개/날짜)")
for r in (0.0, rho, lo, hi, 0.5):
    mult = (1 + r) / 2
    td = KR_ONLY_TD * mult
    print(f"  ρ={r:+.3f} → 날짜당 실효관측 {2/(1+r):.2f}개 · 필요 {td:.0f}거래일 "
          f"({td/21:.1f}개월) · 단축 {KR_ONLY_TD - td:.0f}거래일")
