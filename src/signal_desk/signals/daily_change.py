"""어제와 달라진 것 — 매일 열 이유.

## 왜 이 형태인가

"오늘 살 것"은 데일리 훅이 될 수 없다. 실측으로 매수권이 **0건인 날이 대부분**이고
(정밀도 우선 설계의 정상 결과다) 없는 날 억지로 뭘 보여주면 그게 곧 거짓이다.
반면 **변화는 매일 반드시 있거나, 없다는 사실 자체가 정보**다.

## 인과를 정직하게 말한다

이 리포가 명시적으로 경계하는 것: *"설명과 결정이 다른 데이터를 쓰면 사후 합리화가 된다 —
점수는 8팩터로 나오는데 뉴스를 붙이면 '이 기사 때문에 관망'이라는 없는 인과가 만들어진다."*

그래서 등급이 **실제로** 무엇 때문에 바뀌었는지만 말한다. 네 가지뿐이다:

- `factor`  — 점수가 내려갔다(어느 팩터가 내려갔는지 함께)
- `rank`    — **점수는 올랐는데** 다른 종목이 더 올라 순위에서 밀렸다
- `gate`    — 안전장치가 새로 걸렸다(또는 풀렸다)
- `coverage`— 볼 수 있는 관점이 줄어 매수 대상에서 빠졌다

`rank` 가 특히 중요하다 — 실측에서 KCC가 점수 `+1.81 → +2.04` 로 **오르고도** 강등됐다.
그게 "왜 3.0인데 관망이고 1.72인데 강력매수냐"는 혼란의 정체다.

뉴스·공시는 **맥락**으로만 붙이고 원인으로 쓰지 않는다(호출자가 라벨을 분리해 렌더한다).
"""

from __future__ import annotations

# 등급 순서 — 승격/강등 방향을 판정한다.
_ORDER = {"STRONG_SELL": 0, "SELL": 1, "HOLD": 2, "BUY": 3, "STRONG_BUY": 4}
_KIND_KO = {"STRONG_BUY": "강력매수", "BUY": "매수", "HOLD": "관망",
            "SELL": "매도", "STRONG_SELL": "강력매도"}
# 점수 변화가 이보다 작으면 "점수 때문"이라 부르지 않는다 — 부호가 아니라 크기로 본다.
_SCORE_EPS = 0.05
# 팩터 변화 표기 하한(같은 이유).
_FACTOR_EPS = 0.05
_FACTORS = ("technical", "fundamental", "valuation", "reversion",
            "flow", "quality", "momentum", "short")


def _num(v) -> float | None:
    """pandas 결손(NaN)·None 을 None 으로 통일. NaN 은 유효 JSON 이 아니다."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _cause(prev: dict, cur: dict, *, demoted: bool,
           all_prev: dict | None = None, all_cur: dict | None = None) -> dict:
    """등급 변화의 **실제** 원인. 뉴스는 보지 않는다(그건 맥락이다).

    `demoted` 가 필요한 이유: **점수가 반대 방향으로 움직였으면 그게 원인일 수 없다.**
    실측 KCC가 `+1.81 → +2.04` 로 오르고도 강등됐는데, 점수 변화만 보면 `factor` 로
    분류된다 — 그건 거짓이다(점수는 올랐으니까). 그 경우는 상대 순위다.
    """
    ps, cs = _num(prev.get("score")), _num(cur.get("score"))
    d_score = None if (ps is None or cs is None) else round(cs - ps, 2)

    pg, cg = bool(prev.get("gate_blocked")), bool(cur.get("gate_blocked"))
    plc, clc = bool(prev.get("low_coverage")), bool(cur.get("low_coverage"))

    # 게이트가 새로 걸렸다/풀렸다 — 점수와 무관하게 자격을 바꾼다.
    if cg and not pg:
        return {"kind": "gate", "text": "안전장치가 새로 걸렸습니다"}
    if pg and not cg:
        return {"kind": "gate", "text": "안전장치가 풀렸습니다"}
    # 볼 수 있는 관점이 줄어 매수 대상에서 빠졌다(점수는 오히려 부풀려진다 — X2).
    if clc and not plc:
        return {"kind": "coverage", "text": "볼 수 있는 관점이 줄어 매수 대상에서 빠졌습니다"}
    if plc and not clc:
        return {"kind": "coverage", "text": "볼 수 있는 관점이 늘어 매수 대상에 들어왔습니다"}

    # 점수가 실제로 움직였나 — **등급과 같은 방향일 때만** 원인으로 인정한다.
    # 강등인데 점수가 올랐다(또는 승격인데 내려갔다)면 점수는 원인이 아니다.
    same_way = (d_score is not None
                and ((demoted and d_score < 0) or (not demoted and d_score > 0)))
    if same_way and abs(d_score) >= _SCORE_EPS:
        # **크기순으로 정렬하면 안 된다** — PIT 컬럼은 8개 중 5개가 정규화가 아니다
        # (`valuation`=백분위 · `quality`=점 · `momentum`=수익률 · `short`=비중 · `flow`=원자료).
        # 실측(HD현대): `주가가 싼가 -4.60`(백분위)이 `차트 흐름 -0.30`(정규화)을 이겨 1위였다.
        # `why_now.rank_factor_moves` 와 **같은 구현을 공유한다** — 통계를 두 곳에 두면 갈라진다.
        from signal_desk.signals.why_now import rank_factor_moves
        moved = rank_factor_moves(prev, cur, all_a=all_prev or {}, all_b=all_cur or {}, limit=3)
        return {"kind": "factor", "text": f"점수가 {d_score:+.2f} 움직였습니다",
                "factors": moved}

    # 점수가 거의 그대로거나 **반대로** 움직였는데 등급이 바뀌었다 → 상대 순위다.
    # **이게 가장 헷갈리는 경우다** — 실측 KCC: +1.81 → +2.04 인데 강등.
    if demoted and d_score is not None and d_score > 0:
        txt = f"점수가 {d_score:+.2f} **올랐는데도** 다른 종목이 더 올라 순위에서 밀렸습니다"
    elif not demoted and d_score is not None and d_score < 0:
        txt = f"점수가 {d_score:+.2f} **내려갔는데도** 다른 종목이 더 내려가 순위가 올랐습니다"
    else:
        txt = ("점수는 " + (f"{d_score:+.2f} " if d_score is not None else "")
               + "거의 그대로인데 **다른 종목의 순위가 바뀌어** 자리가 달라졌습니다")
    return {"kind": "rank", "text": txt}


def diff(history_rows: list[dict], *, names: dict[str, str] | None = None,
         limit: int = 6) -> dict:
    """가장 최근 두 PIT 스냅샷을 비교한다.

    `history_rows` 는 `store.load_signal_history().to_dict("records")`.
    반환에 `ready`·`blocked_reason` 을 둔다 — 0의 이유를 말한다(스냅샷 1일뿐 등).
    """
    dates = sorted({str(r.get("date")) for r in history_rows if r.get("date")})
    if len(dates) < 2:
        return {"ready": False, "dates": dates,
                "blocked_reason": ("비교할 스냅샷이 하루뿐입니다 — 마감 스냅샷이 이틀 이상 "
                                   "쌓이면 어제와 달라진 것을 보여줍니다")}
    d_prev, d_cur = dates[-2], dates[-1]
    prev = {str(r["ticker"]): r for r in history_rows if str(r.get("date")) == d_prev}
    cur = {str(r["ticker"]): r for r in history_rows if str(r.get("date")) == d_cur}
    nm = names or {}

    changes: list[dict] = []
    for t, c in cur.items():
        p = prev.get(t)
        if not p:
            continue
        pk, ck = str(p.get("kind") or ""), str(c.get("kind") or "")
        if pk == ck or pk not in _ORDER or ck not in _ORDER:
            continue
        demoted = _ORDER[ck] < _ORDER[pk]
        cause = _cause(p, c, demoted=demoted, all_prev=prev, all_cur=cur)
        changes.append({
            "ticker": t, "name": nm.get(t) or t,
            "from": pk, "to": ck,
            "from_ko": _KIND_KO.get(pk, pk), "to_ko": _KIND_KO.get(ck, ck),
            "direction": "down" if demoted else "up",
            "score_before": _num(p.get("score")), "score_after": _num(c.get("score")),
            "cause": cause,
        })
    # 승격을 먼저, 그 안에서 점수 높은 순 — 볼 만한 것이 위로.
    changes.sort(key=lambda x: (x["direction"] != "up", -(x["score_after"] or -9)))

    def _buyable(rows: dict) -> int:
        return sum(1 for r in rows.values() if str(r.get("kind")) in ("BUY", "STRONG_BUY"))

    buy_prev, buy_cur = _buyable(prev), _buyable(cur)
    return {
        "ready": True,
        "prev_date": d_prev, "cur_date": d_cur,
        "compared": len(set(prev) & set(cur)),
        "changes": changes[:limit],
        "changes_total": len(changes),
        "buyable": {"before": buy_prev, "after": buy_cur, "delta": buy_cur - buy_prev},
        # 변화가 없으면 그 사실을 말한다 — 매일 장문이면 안 읽힌다.
        "quiet": not changes and buy_prev == buy_cur,
        "basis": ("두 PIT 스냅샷(마감 저장)의 등급 비교. 원인은 점수·순위·안전장치·관점 커버리지 "
                  "**넷 중 하나**로만 말합니다 — 뉴스를 원인으로 쓰면 없는 인과가 만들어집니다."),
    }
