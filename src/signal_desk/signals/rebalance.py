"""실보유 리밸런싱 제안 — 내 보유종목을 시그널·성향 목표배분에 맞춰 유지/축소/매도/추가 제안.

결정론적으로 액션을 정하고(시그널 + 목표비중 대비 과부족), LLM은 '왜'를 설명하는 해설만 붙인다
(가드레일: 실행/수량은 유저 몫, 우리는 제안만). LLM 없으면 규칙기반 해설로 폴백.
"""

from __future__ import annotations

from signal_desk import llm
from signal_desk.signals import engine


def propose(holdings: list[dict], signal_by_ticker: dict, prices: dict[str, list[float]],
            names: dict[str, str], style_params: dict) -> dict:
    """holdings: [{ticker, qty, avg_price}]. 반환: {actions, adds, total_value, target_weight, keep_n}."""
    target_w = style_params["position_pct"]
    target_n = style_params["max_positions"]

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
        if engine.is_sell(kind):
            action, reason = "매도", (f"시그널 {kind}(점수 {score:+.2f}) — 비중 정리 권고")
        elif w > target_w * 1.6:
            action, reason = "축소", (f"비중 과다({w * 100:.0f}% ≫ 목표 {target_w * 100:.0f}%) — 일부 차익/분산")
            keep_n += 1
        elif engine.is_buy(kind) and w < target_w * 0.7:
            action, reason = "비중확대", (f"시그널 {kind}(점수 {score:+.2f}) + 저비중({w * 100:.0f}%) — 목표까지 추가 여지")
            keep_n += 1
        else:
            action, reason = "유지", (f"시그널 {kind or '정보없음'} · 비중 {w * 100:.0f}%(목표 {target_w * 100:.0f}%)")
            keep_n += 1
        actions.append({"ticker": r["ticker"], "name": r["name"], "kind": kind, "score": score,
                        "action": action, "reason": reason, "weight": round(w * 100, 1),
                        "pl_pct": round(pl, 1), "value": round(r["value"])})

    # 신규 편입 제안: 미보유 강한 BUY를 점수순으로, 목표 종목수까지
    held = {h["ticker"] for h in holdings}
    slots = max(0, target_n - keep_n)
    strong = sorted((s for t, s in signal_by_ticker.items()
                     if engine.is_buy(s.kind) and s.score >= style_params["min_buy_score"] and t not in held),
                    key=lambda s: s.score, reverse=True)[:slots]
    adds = [{"ticker": s.ticker, "name": s.name, "score": s.score, "kind": s.kind,
             "reason": f"미보유 {s.kind}(점수 {s.score:+.2f}) — 목표배분 채움(약 {target_w * 100:.0f}%)"}
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
                  "2~3문장으로 쉽게 설명한다. 추천·단정 대신 근거 중심. 숫자를 지어내지 마라.")
        user = (f"[성향] {style_label} · [시장] 국면 {context.get('regime')}/거시 {context.get('macro_bias')}\n"
                f"매도 권고: {sells or '없음'} / 축소: {trims or '없음'} / 신규 편입: {adds or '없음'} / "
                f"목표 종목당 비중 {plan['target_weight']}%\n왜 이렇게 조정하는지 설명해줘.")
        out = llm.complete(system, user, max_tokens=400)
        if out:
            return out
    parts = []
    if sells:
        parts.append(f"시그널이 꺾인 {', '.join(sells)}는 정리")
    if trims:
        parts.append(f"과다 비중 {', '.join(trims)}는 축소")
    if adds:
        parts.append(f"강한 BUY {', '.join(adds)} 신규 편입")
    return (f"{style_label} 기준 " + ", ".join(parts) + "을 제안합니다.") if parts else \
        f"{style_label} 기준 현재 보유가 대체로 목표배분에 부합합니다."
