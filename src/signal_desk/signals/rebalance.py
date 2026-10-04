"""실보유 리밸런싱 제안 — 내 보유종목을 시그널·성향 목표배분에 맞춰 유지/축소/매도/추가 제안.

결정론적으로 액션을 정하고(시그널 + 목표비중 대비 과부족), LLM은 '왜'를 설명하는 해설만 붙인다
(가드레일: 실행/수량은 유저 몫, 우리는 제안만). LLM 없으면 규칙기반 해설로 폴백.
"""

from __future__ import annotations

from signal_desk import llm
from signal_desk.copy_style import PLAIN_KOREAN
from signal_desk.signals import engine


# 목표비중 ±밴드 — 이 안이면 유지(라오어 VR 응용: 밴드 벗어날 때만 리밸런싱 → 잦은 매매·노이즈 방지)
REBAL_BAND = 0.25
_SIGNAL_LABEL = {"STRONG_BUY": "강력매수", "BUY": "매수", "HOLD": "관망",
                 "SELL": "매도", "STRONG_SELL": "강력매도"}


def propose(holdings: list[dict], signal_by_ticker: dict, prices: dict[str, list[float]],
            names: dict[str, str], style_params: dict) -> dict:
    """holdings: [{ticker, qty, avg_price}]. 반환: {actions, adds, total_value, target_weight, keep_n}."""
    target_w = style_params["position_pct"]
    target_n = style_params["max_positions"]
    band_hi, band_lo = target_w * (1 + REBAL_BAND), target_w * (1 - REBAL_BAND)  # 밴드 상·하단

    rows = []
    total = 0.0
    for h in holdings:
        t = h["ticker"]
        closes = prices.get(t)
        price = closes[-1] if closes else h["avg_price"]
        value = h["qty"] * price
        total += value
        rows.append({"ticker": t, "name": names.get(t, t), "qty": h["qty"], "avg_price": h["avg_price"],
                     "price": price, "value": value, "sig": signal_by_ticker.get(t)})

    actions = []
    keep_n = 0
    for r in rows:
        w = (r["value"] / total) if total else 0.0
        sig = r["sig"]
        kind = sig.kind if sig else None
        score = sig.score if sig else None
        pl = (r["price"] / r["avg_price"] - 1) * 100 if r["avg_price"] else 0.0
        band = f"목표 {target_w * 100:.0f}% (유지 범위 {band_lo * 100:.1f}~{band_hi * 100:.1f}%)"
        signal = _SIGNAL_LABEL.get(kind, "신호 없음")
        if engine.is_sell(kind):
            action, reason = "매도", f"{signal} 판정(점수 {score:+.2f})이 나왔습니다. 보유 이유를 다시 확인하세요."
        elif w > band_hi:  # 밴드 상단 초과 → 과다
            action, reason = "축소", f"현재 비중 {w * 100:.1f}%가 {band}를 넘었습니다. 일부 축소를 검토하세요."
            keep_n += 1
        elif engine.is_buy(kind) and w < band_lo:  # 밴드 하단 미만 + BUY → 채움
            action, reason = "비중확대", f"{signal} 판정(점수 {score:+.2f})이고 현재 비중 {w * 100:.1f}%가 {band}보다 낮습니다."
            keep_n += 1
        else:  # 밴드 내 → 유지(리밸런싱 안 함)
            action, reason = "유지", f"{signal} 판정이며 현재 비중 {w * 100:.1f}%입니다. {band}를 기준으로 지켜봅니다."
            keep_n += 1
        actions.append({"ticker": r["ticker"], "name": r["name"], "kind": kind, "score": score,
                        "action": action, "reason": reason, "weight": round(w * 100, 1),
                        "pl_pct": round(pl, 1), "value": round(r["value"])})

    # 신규 편입 제안: 미보유 강한 BUY를 점수순으로, 목표 종목수까지
    held = {h["ticker"] for h in holdings}
    slots = max(0, target_n - keep_n)
    strong = sorted((s for t, s in signal_by_ticker.items()
                     if engine.is_buy(s.kind) and s.score >= style_params["min_buy_score"]
                     and t not in held and not getattr(s, "event_risk", False)),
                    key=lambda s: s.score, reverse=True)[:slots]
    adds = [{"ticker": s.ticker, "name": s.name, "score": s.score, "kind": s.kind,
             "reason": f"아직 보유하지 않은 종목입니다. {_SIGNAL_LABEL.get(s.kind, s.kind)} 판정(점수 {s.score:+.2f}), 목표 비중 약 {target_w * 100:.0f}%."}
            for s in strong]

    return {"actions": actions, "adds": adds, "total_value": round(total),
            "target_weight": round(target_w * 100, 1), "target_n": target_n, "keep_n": keep_n}


def explain(plan: dict, style_label: str, context: dict) -> str:
    """리밸런싱 제안의 종합 해설(LLM). 없으면 규칙기반 한 줄."""
    sells = [a["name"] for a in plan["actions"] if a["action"] == "매도"]
    trims = [a["name"] for a in plan["actions"] if a["action"] == "축소"]
    adds = [a["name"] for a in plan["adds"]]
    if llm.available():
        system = ("너는 한국 주식 포트폴리오 자문역이다. 아래 리밸런싱 제안을 성향과 시장 맥락에 비추어 "
                  "2~3문장으로 설명한다. 추천·단정 대신 근거 중심. 숫자를 지어내지 마라. " + PLAIN_KOREAN)
        user = (f"[성향] {style_label} · [시장] 국면 {context.get('regime')}/거시 {context.get('macro_bias')}\n"
                f"매도 권고: {sells or '없음'} / 축소: {trims or '없음'} / 신규 편입: {adds or '없음'} / "
                f"목표 종목당 비중 {plan['target_weight']}%\n왜 이렇게 조정하는지 설명해줘.")
        out = llm.complete(system, user, max_tokens=400, purpose="rebalance")
        if out:
            return out
    parts = []
    if sells:
        parts.append(f"{', '.join(sells)}의 매도 판정을 다시 확인하세요.")
    if trims:
        parts.append(f"비중이 높은 {', '.join(trims)}의 축소를 검토하세요.")
    if adds:
        parts.append(f"{', '.join(adds)}의 신규 편입을 검토하세요.")
    return (f"{style_label} 기준입니다. " + " ".join(parts)) if parts else \
        f"{style_label} 기준에서 현재 보유 비중은 대체로 목표 범위에 있습니다."
