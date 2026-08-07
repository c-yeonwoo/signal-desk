"""왜 **지금** 이 종목인가 — 섹터인가 종목 고유인가.

## 이 모듈이 답하는 질문

"어제와 달라진 것"(`daily_change`)은 **하루** 변화를 넷으로 분류한다. 그런데 사용자가 실제로
묻는 것은 *"왜 요즘 갑자기 이 섹터, 이 종목이 발동됐나"* 다 — **며칠~몇 주**의 궤적이고,
가장 중요한 갈림길은 **섹터 전체가 움직였나, 이 종목만 움직였나**다. 그 둘은 완전히 다른 뜻이다:

- 섹터 전체 → 거시·업종 이벤트. 이 종목을 고른 것이 아니라 섹터를 고른 것에 가깝다.
- 이 종목만 → 종목 고유 사유(실적·수급·공시). 팩터가 실제로 이 종목을 골라냈다.

## LLM을 쓰지 않는다

전부 PIT 스냅샷 산술이다. 뉴스·거시 해설(맥락)은 **호출자가 따로 붙이고 라벨을 나눈다** —
이 리포가 경계하는 것: *"설명과 결정이 다른 데이터를 쓰면 사후 합리화가 된다."* 여기서 나오는
숫자는 **점수를 실제로 만든 입력**이므로 근거이고, 그래서 지어낼 여지가 없다.

## 지어내지 않는 것

- **인과**: "섹터가 올라서 이 종목이 올랐다"고 말하지 않는다. 같이 올랐다는 **사실**만 낸다.
- **없는 표본**: 같은 섹터 종목이 3개 미만이면 판정하지 않고 이유를 낸다(`peers_too_few`).
- **전망**: 앞으로 어떻게 될지는 다루지 않는다.
"""

from __future__ import annotations

_FACTORS = ("technical", "fundamental", "valuation", "reversion",
            "flow", "quality", "momentum", "short")
_FACTOR_KO = {
    "technical": "차트 흐름", "fundamental": "실적·재무", "valuation": "주가가 싼가",
    "reversion": "많이 떨어졌나", "flow": "누가 사고 있나", "quality": "회사 체질",
    "momentum": "오르는 추세", "short": "공매도 압력",
}
_BUY_KINDS = ("BUY", "STRONG_BUY")
# 팩터 이동 표기 하한 — 부호가 아니라 크기로 본다(레포 규칙).
_FACTOR_EPS = 0.05
# 섹터 판정 규칙. **문턱을 만들 때는 무엇을 재는지 이름으로 못박는다.**
# `share = 섹터 동료 중위 변화 / 이 종목 변화` — 1에 가까우면 섹터가 통째로 움직인 것이다.
_SECTOR_SHARE = 0.5      # 이 이상이면 섹터 전체
_IDIO_SHARE = 0.2        # 이 이하면 종목 고유
_MIN_PEERS = 3           # 이보다 적으면 판정하지 않는다(중위가 의미 없다)
# 창 안에서 "움직였다"고 부를 최소 점수 변화. 이보다 작으면 궤적을 설명하지 않는다 —
# 없는 변화에 이야기를 붙이면 그게 사후 합리화다.
_MIN_SCORE_MOVE = 0.15


def _num(v) -> float | None:
    """pandas 결손(NaN)·None 을 None 으로 통일. NaN 은 유효 JSON 이 아니다."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _sector_verdict(ticker_delta: float, peer_deltas: list[float]) -> dict:
    """섹터가 통째로 움직였나, 이 종목만인가. **인과가 아니라 동반 여부**다.

    `share` 를 노출한다 — 판정만 내면 왜 그렇게 갈랐는지 모른다(문턱은 값과 함께 낸다).
    """
    n = len(peer_deltas)
    if n < _MIN_PEERS:
        return {"verdict": "peers_too_few", "peers_n": n, "peer_median_delta": None,
                "share": None,
                "text": f"같은 섹터에 비교할 종목이 {n}개뿐이라 섹터인지 종목 고유인지 "
                        f"가리지 않았습니다(최소 {_MIN_PEERS}개 필요)"}
    med = _median(peer_deltas)
    if abs(ticker_delta) < 1e-9:
        return {"verdict": "peers_too_few", "peers_n": n, "peer_median_delta": round(med, 2),
                "share": None,
                "text": "이 종목의 점수 변화가 사실상 0이라 섹터와 비교하지 않았습니다"}
    share = med / ticker_delta
    if share >= _SECTOR_SHARE:
        v, txt = "sector", (
            f"같은 섹터 {n}종목의 중위 점수도 {med:+.2f} 움직였습니다 — "
            f"이 종목만이 아니라 **섹터 전체**가 같이 갔습니다")
    elif share <= _IDIO_SHARE:
        v, txt = "idiosyncratic", (
            f"같은 섹터 {n}종목의 중위 점수는 {med:+.2f}에 그쳤습니다 — "
            f"**이 종목 고유**의 움직임입니다")
    else:
        v, txt = "mixed", (
            f"같은 섹터 {n}종목의 중위 점수는 {med:+.2f}입니다 — "
            f"섹터 흐름과 종목 고유 요인이 **섞여** 있습니다")
    return {"verdict": v, "peers_n": n, "peer_median_delta": round(med, 2),
            "share": round(share, 2), "text": txt}


def explain(history_rows: list[dict], ticker: str, *,
            sector_of=None, name: str | None = None, window: int = 10) -> dict:
    """`ticker` 가 **최근 창에서** 왜 지금인지. `history_rows` 는 PIT 스냅샷 레코드.

    `sector_of(ticker) -> str|None` 로 섹터를 받는다(주입 — 이 모듈이 섹터 소스를 모른다).
    `window` 는 거래일 수(스냅샷 날짜 수)다.
    """
    dates = sorted({str(r.get("date")) for r in history_rows if r.get("date")})
    if len(dates) < 2:
        return {"ready": False, "ticker": ticker,
                "blocked_reason": "비교할 마감 스냅샷이 하루뿐입니다 — 이틀 이상 쌓이면 "
                                  "며칠 사이 무엇이 움직였는지 보여줍니다"}
    win = dates[-window:] if window and len(dates) > window else dates
    d0, d1 = win[0], win[-1]
    by_date: dict[str, dict[str, dict]] = {}
    for r in history_rows:
        d = str(r.get("date"))
        if d in set(win):
            by_date.setdefault(d, {})[str(r.get("ticker"))] = r

    a, b = by_date.get(d0, {}).get(ticker), by_date.get(d1, {}).get(ticker)
    if not a or not b:
        return {"ready": False, "ticker": ticker,
                "blocked_reason": f"이 종목이 {d0}~{d1} 스냅샷에 다 있지 않습니다 "
                                  "(유니버스에 새로 들어왔거나 빠진 날이 있습니다)"}

    s0, s1 = _num(a.get("score")), _num(b.get("score"))
    if s0 is None or s1 is None:
        return {"ready": False, "ticker": ticker,
                "blocked_reason": "창의 양 끝 점수가 비어 있습니다"}
    delta = round(s1 - s0, 2)

    # ── 팩터별 이동: 무엇이 점수를 밀었나(크기 순) ──
    moved = []
    for f in _FACTORS:
        x, y = _num(a.get(f)), _num(b.get(f))
        if x is None or y is None or abs(y - x) < _FACTOR_EPS:
            continue
        moved.append({"factor": f, "label": _FACTOR_KO.get(f, f),
                      "before": round(x, 2), "after": round(y, 2),
                      "delta": round(y - x, 2)})
    moved.sort(key=lambda m: -abs(m["delta"]))

    # ── 전환일: 매수권으로 바뀐 날 / 점수가 가장 크게 뛴 날 ──
    turned_on, prev_kind = None, str(a.get("kind") or "")
    jump_date, jump = None, 0.0
    prev_score = s0
    for d in win[1:]:
        row = by_date.get(d, {}).get(ticker)
        if not row:
            continue
        k = str(row.get("kind") or "")
        if k in _BUY_KINDS and prev_kind not in _BUY_KINDS:
            turned_on = d
        prev_kind = k or prev_kind
        sc = _num(row.get("score"))
        if sc is not None:
            if abs(sc - prev_score) > abs(jump):
                jump, jump_date = round(sc - prev_score, 2), d
            prev_score = sc

    # ── 섹터 동반 여부 — 같은 섹터 종목의 **같은 창** 점수 변화 ──
    sec = sector_of(ticker) if sector_of else None
    peer_deltas: list[float] = []
    if sec and sector_of:
        for tk, row1 in by_date.get(d1, {}).items():
            if tk == ticker or sector_of(tk) != sec:
                continue
            row0 = by_date.get(d0, {}).get(tk)
            if not row0:
                continue                       # 창 양 끝에 다 있는 종목만(같은 구간 비교)
            p0, p1 = _num(row0.get("score")), _num(row1.get("score"))
            if p0 is None or p1 is None:
                continue
            peer_deltas.append(p1 - p0)
    sector = (_sector_verdict(delta, peer_deltas) if sec
              else {"verdict": "no_sector", "peers_n": 0, "peer_median_delta": None,
                    "share": None, "text": "섹터 정보가 없어 섹터 비교를 하지 않았습니다"})
    sector["name"] = sec

    quiet = abs(delta) < _MIN_SCORE_MOVE
    return {
        "ready": True,
        "ticker": ticker, "name": name or ticker,
        "from_date": d0, "to_date": d1, "window_dates": len(win),
        "score_before": round(s0, 2), "score_after": round(s1, 2), "score_delta": delta,
        "kind_before": str(a.get("kind") or ""), "kind_after": str(b.get("kind") or ""),
        "turned_buy_on": turned_on,
        "biggest_move": ({"date": jump_date, "delta": jump} if jump_date else None),
        "factors": moved[:4],
        "sector": sector,
        # 변화가 거의 없으면 **이야기를 붙이지 않는다** — 없는 변화에 설명을 달면 사후 합리화다.
        "quiet": quiet,
        "quiet_reason": (f"최근 {len(win)}거래일 점수 변화가 {delta:+.2f}로 거의 없습니다 — "
                         f"설명할 움직임이 없습니다" if quiet else None),
        "basis": (f"PIT 마감 스냅샷 {d0}~{d1}({len(win)}거래일)의 점수·관점 변화. 섹터 판정은 "
                  f"같은 섹터 종목의 **같은 창** 중위 변화 대비 비율(share)이며, 인과가 아니라 "
                  f"동반 여부입니다. 뉴스·거시는 여기 쓰지 않습니다(맥락은 따로 표시)."),
    }
