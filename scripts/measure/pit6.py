"""로컬에서 6팩터 PIT 하네스를 돌리기 위한 실험대.

로컬 `fundamentals.json` 이 1종목뿐이라 `store.pit_fund_scores` 가 발행주식수 근사에서
막힌다. 여기서는 **PIT 시총 앵커**(universe_history.json, 305종목 × 60스냅샷)로 주식수를
역산해 같은 경로를 탄다 — 프로덕션에서는 fundamentals.json 이 신선해서 이 우회가 필요없다.
"""
import os, pathlib, sys, json
os.chdir(pathlib.Path(__file__).resolve().parents[2])  # 리포 루트
sys.path.insert(0, "src")
from signal_desk import store
from signal_desk.signals import harness as hz
from signal_desk.signals import pit_fundamentals as pf


def build(sc, growth_weight: float = 0.0, full_denominator: bool = False):
    """(panel, scores, cov, fired, covers) — store.pit_fund_scores 와 같은 조립."""
    uni = store.load_universe()
    panel = hz.build_panel(store.load_all_dated_closes(), {u["ticker"] for u in uni})
    hist = store.load_fundamentals_history()
    uni_hist = store.load_universe_history()

    # 발행주식수: 최신 앵커 시총 ÷ 그 날 종가 (fundamentals.json 대체)
    anchors = pf.mktcap_anchors(uni_hist)
    idx = {d: i for i, d in enumerate(panel.dates)}
    shares = {}
    for t, pairs in anchors.items():
        row = panel.closes.get(t)
        if not row:
            continue
        for d, mc in reversed(pairs):
            i = idx.get(d)
            px = row[i] if i is not None else None
            if mc and px and px > 0:
                shares[t] = mc / px
                break

    _cache = {}
    def uni_at(date_str):
        if date_str not in _cache:
            items = store.universe_at(date_str)
            _cache[date_str] = {u["ticker"] for u in items} if items else None
        return _cache[date_str]

    pit_tickers = {u["ticker"] for u in store.pit_universe_tickers()}
    panel = hz.build_panel(store.load_all_dated_closes(), pit_tickers)
    idx = {d: i for i, d in enumerate(panel.dates)}
    anchor_days = sorted(uni_hist)
    price_on = {t: {d: row[idx[d]] for d in anchor_days if d in idx and row[idx[d]] is not None}
                for t, row in panel.closes.items()}
    scores, cov6, fired6, meta6, covers = hz.scores_with_pit_fundamentals(
        panel, sc, hist, shares=shares, universe=uni, universe_at=uni_at,
        mktcap_anchors=anchors, price_on=price_on, growth_weight=growth_weight,
        full_denominator=full_denominator)
    return panel, scores, cov6, fired6, meta6, covers


if __name__ == "__main__":
    from signal_desk import signalcfg
    sc = signalcfg.get_config()
    panel, scores, cov6, fired6, meta6, covers = build(sc)
    n = sum(1 for v in scores.values() if any(x is not None for x in v))
    print(f"패널 {len(panel)}일 · 점수 있는 종목 {n}/{len(scores)}")
    print("팩터 계산가능률:", cov6)
    print("팩터 발동률:", fired6)
    print("메타:", {k: v for k, v in meta6.items() if k != "error"})
