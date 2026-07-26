"""KB 정보 커버리지 shadow — "근거 문서가 있는 매수 후보가 더 나았나"를 실측으로 잰다.

동기: KB에는 게이트를 통과한 문서가 쌓이는데(출처 등록·스팸·무관 심사) 그 게이트는 **형식적
신뢰도**만 본다 — 주장이 맞았는지는 한 번도 채점되지 않았다. 그래서 "강한 기준을 통과했으니
믿을만하다"는 근거가 없다. 이 모듈은 **기존 데이터만으로(LLM 비용 0)** 첫 검증을 한다:
같은 날 매수 판정을 받은 종목을 원문 문서 수로 나눠 이후 실현수익률을 비교한다.

판정 규칙은 만들지 않는다 — `accuracy.diff_verdict`(표본 수가 아니라 유의성)을 그대로 쓴다.
점수·봇에는 반영하지 않는다(shadow). 편향은 숨기지 않고 `caveats`로 함께 내보낸다:

1. **prune 과소집계** — 과거 날짜의 문서 수를 `fetched`로 재구성하면 이미 지워진 문서가 빠진다.
   그래서 오늘부터는 PIT 스냅샷의 `kb_docs` 열을 쓰고(store.snapshot_signals), 그 열이 없는
   과거 행만 재구성값으로 채우며 몇 행이 재구성인지 함께 보고한다.
2. **수집 대상 교락** — KB 수집 대상(`_kb_targets`)이 매수권·보유·관심을 우선하므로 문서 유무는
   "뉴스가 있었나"뿐 아니라 "우리가 수집했나"도 반영한다. 따라서 이 비교는 정보 유무의 인과가
   아니라 **정보가 붙는 종목의 사후 성과 차이**다. 이 값으로 파라미터를 바꾸지 않는다.
"""

from __future__ import annotations

from typing import Any

from signal_desk.signals import accuracy
from signal_desk.signals.engine import is_buy

DENSE_MIN_DOCS = 3          # '근거 있음'으로 볼 최소 원문 수(다이제스트 하한 12건보다 낮게 시작)
PRIMARY_HORIZON = 20
MIN_SAMPLES = 20


def _doc_counts_for(dates: list[str]) -> dict[str, dict[str, int]]:
    """date -> {ticker: 문서수} 재구성(PIT 열이 없는 과거 행 보정용).

    날짜 하나당 쿼리 한 번. 재구성이라 prune으로 지워진 문서는 빠진다(caveats 참고)."""
    import datetime

    from signal_desk import db

    out: dict[str, dict[str, int]] = {}
    for d in dates:
        try:
            ts = datetime.datetime.fromisoformat(d + "T23:59:59").timestamp()
        except ValueError:
            continue
        out[d] = db.kb_doc_counts(before_ts=ts)
    return out


def shadow(closes_by_ticker: dict[str, tuple[list[str], list[float]]], *,
           horizon: int = PRIMARY_HORIZON, min_samples: int = MIN_SAMPLES,
           dense_min: int = DENSE_MIN_DOCS) -> dict[str, Any]:
    """매수 판정 종목을 'KB 원문 있음/없음'으로 갈라 horizon 실현수익률을 비교한다."""
    from signal_desk import store

    df = store.load_signal_history()
    if df.empty or not {"date", "ticker", "kind"} <= set(df.columns):
        return {"ready": False, "blocked_reason": "PIT 스냅샷 없음 — 마감 후 스냅샷이 쌓이면 판정 가능"}

    has_pit = "kb_docs" in df.columns
    rows = df.to_dict("records")
    buys = [r for r in rows if is_buy(str(r.get("kind") or ""))]
    if not buys:
        return {"ready": False, "blocked_reason": "스냅샷에 매수 판정이 없다 — 후보가 비는 원인부터 확인"}

    need_recon = sorted({str(r["date"]) for r in buys
                         if not has_pit or r.get("kb_docs") is None})
    recon = _doc_counts_for(need_recon) if need_recon else {}

    dense: list[float] = []
    thin: list[float] = []
    n_recon = n_pit = 0
    for r in buys:
        ticker, date = str(r.get("ticker") or ""), str(r.get("date") or "")
        series = closes_by_ticker.get(ticker)
        if not series:
            continue
        ret = accuracy.forward_returns(series[0], series[1], date, (horizon,)).get(horizon)
        if ret is None:
            continue                                  # 미성숙 — 표본에서 뺀다(정직한 표본)
        n = r.get("kb_docs")
        if has_pit and n is not None:
            n_pit += 1
            docs = int(n)
        else:
            n_recon += 1
            docs = int((recon.get(date) or {}).get(ticker, 0))
        if docs >= dense_min:
            dense.append(ret)
        elif docs == 0:
            thin.append(ret)                          # 중간(1~2건)은 어느 쪽도 아니라 버린다

    v = accuracy.diff_verdict(dense, thin, min_samples=min_samples)
    # 0에는 이유를 붙인다 — '아직 안 익어서 0'과 '매수 판정 자체가 없어서 0'은 다른 상태다.
    if v["matured"] == 0 and len(buys) < min_samples:
        v["blocked_reason"] = (f"매수 판정 행 {len(buys)}개 — 표본이 스냅샷에 없다"
                              f"(순위 기준 전환 후 새로 쌓이는 중)")
    caveats = [
        "수집 대상이 매수권·보유·관심을 우선하므로 문서 유무에는 '우리가 수집했나'가 섞여 있다 — "
        "정보의 인과가 아니라 정보가 붙는 종목의 사후 성과 차이다.",
    ]
    if n_recon:
        caveats.append(
            f"{n_recon}행은 문서 수를 `fetched`로 재구성했다(PIT {n_pit}행). prune이 지운 문서는 "
            "빠져서 과거일수록 과소집계된다 — 오늘 이후 스냅샷이 쌓이면 이 비율이 줄어든다.")
    return {"ready": v["matured"] > 0, "horizon": horizon, "dense_min_docs": dense_min, **v,
            "buy_rows": len(buys), "pit_rows": n_pit, "reconstructed_rows": n_recon,
            "note": f"같은 날 매수 판정 중 원문 {dense_min}건 이상 vs 0건의 {horizon}거래일 실현수익률 차이. "
                    "판정이 떠도 반영은 사람이 결정한다.",
            "caveats": caveats,
            "disclaimer": "KB 커버리지 shadow · 점수·봇 미반영 · 승격 게이트 아님"}


def coverage_now() -> dict[str, Any]:
    """지금 커버리지 — 대상 종목 중 원문이 있는 비율. 0이 '뉴스가 없어서'인지 '수집이 멈춰서'인지는
    `kb.refresh_status`가 말한다(여기서는 개수만)."""
    from signal_desk import db, store

    counts = db.kb_doc_counts()
    universe = [u["ticker"] for u in store.load_universe()]
    with_docs = [t for t in universe if counts.get(t)]
    digests = db.kb_digests_all()
    return {"universe": len(universe), "with_docs": len(with_docs),
            "with_docs_pct": round(len(with_docs) / len(universe) * 100, 1) if universe else None,
            "digests": len([t for t in digests if t in set(universe)]),
            "docs_total": sum(counts.values())}
