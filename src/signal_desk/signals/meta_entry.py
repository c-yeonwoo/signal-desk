"""메타-진입(shadow) 검증: 기존 매수 후보 중 어느 진입을 *취하지 않을지*만 측정한다.

방향을 다시 예측하거나 점수 가중치를 탐색하지 않는다. PIT 시그널에서 triple-barrier 결과를
만들고, 각 테스트 시점보다 라벨이 먼저 끝난 과거만 학습에 쓰는 purged/embargo walk-forward
확률을 낸다. 표본·OOS 하한이 기준을 채우기 전에는 라이브 매수/알림을 절대 바꾸지 않는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from signal_desk.signals.engine import is_buy


@dataclass(frozen=True)
class TripleBarrierConfig:
    """연구용 라벨 규약. 실행 리스크와 독립이며, 채택 전 사전등록이 필요하다."""
    horizon_days: int = 20
    profit_take_pct: float = 0.10
    stop_loss_pct: float = -0.07


@dataclass(frozen=True)
class BarrierLabel:
    label: int
    exit_offset: int
    exit_reason: str
    return_pct: float


def triple_barrier(entry_price: float, future_prices: Iterable[float],
                   cfg: TripleBarrierConfig) -> BarrierLabel | None:
    """첫 목표/손절/시간 장벽을 라벨링한다. 종가만으로 알 수 없는 장중 순서는 추측하지 않는다."""
    prices = list(future_prices)
    if entry_price <= 0 or len(prices) <= cfg.horizon_days:
        return None
    target = entry_price * (1 + cfg.profit_take_pct)
    stop = entry_price * (1 + cfg.stop_loss_pct)
    for offset, raw in enumerate(prices[1:cfg.horizon_days + 1], start=1):
        price = float(raw)
        if price >= target:
            return BarrierLabel(1, offset, "take_profit", round((price / entry_price - 1) * 100, 4))
        if price <= stop:
            return BarrierLabel(0, offset, "stop_loss", round((price / entry_price - 1) * 100, 4))
    price = float(prices[cfg.horizon_days])
    return BarrierLabel(int(price > entry_price), cfg.horizon_days, "timeout",
                        round((price / entry_price - 1) * 100, 4))


def _bucket_pre_run(value) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if x <= 5:
        return "fresh"
    if x <= 12:
        return "ok"
    if x <= 25:
        return "extended"
    return "late"


def _score_buckets(records: list[dict]) -> dict[tuple[str, str], str]:
    """당일 횡단면 순위로만 score quintile을 만든다. 미래 분포를 써서 버킷을 재정의하지 않는다."""
    by_date: dict[str, list[dict]] = {}
    for r in records:
        if r.get("date") is not None and r.get("ticker") is not None:
            by_date.setdefault(str(r["date"])[:10], []).append(r)
    out = {}
    for day, rows in by_date.items():
        ranked = sorted((r for r in rows if isinstance(r.get("score"), (int, float))),
                        key=lambda r: r["score"])
        n = len(ranked)
        for i, r in enumerate(ranked):
            # 높은 점수 = q5. n=1도 q5로 두어 정보 손실을 만들지 않는다.
            q = min(5, int(i * 5 / max(1, n)) + 1)
            out[(day, str(r["ticker"]))] = f"q{q}"
    return out


def build_labeled_rows(history_records: list[dict], closes_by_ticker: dict[str, tuple[list[str], list[float]]],
                       cfg: TripleBarrierConfig) -> list[dict]:
    """PIT 매수 후보를 시간 장벽 라벨 행으로 바꾼다. 미성숙 미래 구간은 빼며 0으로 채우지 않는다."""
    buckets = _score_buckets(history_records)
    out: list[dict] = []
    for r in history_records:
        if not is_buy(str(r.get("kind") or "")):
            continue
        ticker, day = str(r.get("ticker") or ""), str(r.get("date") or "")[:10]
        series = closes_by_ticker.get(ticker)
        if not ticker or not day or not series:
            continue
        dates, closes = series
        try:
            index = [str(d)[:10] for d in dates].index(day)
            entry = float(closes[index])
        except (ValueError, TypeError, IndexError):
            continue
        label = triple_barrier(entry, closes[index:], cfg)
        if label is None:
            continue
        out.append({
            "ticker": ticker, "date": day, "entry_index": index,
            "label_end_index": index + label.exit_offset, "label": label.label,
            "exit_reason": label.exit_reason, "return_pct": label.return_pct,
            "kind": str(r.get("kind")), "pre_run_bucket": _bucket_pre_run(r.get("pre_run_up_pct")),
            "score_bucket": buckets.get((day, ticker), "unknown"),
        })
    return sorted(out, key=lambda r: (r["entry_index"], r["ticker"]))


def purged_folds(rows: list[dict], *, folds: int = 4, embargo_days: int = 1) -> list[tuple[list[int], list[int]]]:
    """시간순 test 블록과 purge된 train 인덱스.

    train 라벨의 종료가 test 시작 전 embargo까지 끝난 행만 남긴다. 그래서 테스트 기간의 수익을
    일부라도 본 포지션이 학습 행에 들어오는 중첩/누수를 막는다.
    """
    n = len(rows)
    if n < 2 or folds < 2:
        return []
    blocks = min(folds, n)
    result = []
    for f in range(blocks):
        lo, hi = f * n // blocks, (f + 1) * n // blocks
        test = list(range(lo, hi))
        if not test:
            continue
        cutoff = rows[test[0]]["entry_index"] - max(0, embargo_days)
        train = [i for i in range(lo) if rows[i]["label_end_index"] < cutoff]
        result.append((train, test))
    return result


def _group_key(row: dict, fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row.get(f, "unknown")) for f in fields)


def _estimate(train: list[dict], row: dict, fields: tuple[str, ...], *, strength: float = 8.0) -> tuple[float, float, int]:
    """Beta-smoothed group estimate and conservative one-sided lower bound (normal approximation)."""
    wins = sum(int(x["label"]) for x in train)
    n = len(train)
    base = (wins + 1.0) / (n + 2.0) if n else 0.5
    key = _group_key(row, fields)
    group = [x for x in train if _group_key(x, fields) == key]
    gwins, gn = sum(int(x["label"]) for x in group), len(group)
    probability = (gwins + strength * base) / (gn + strength)
    # 95% 단측 하한. 작은 group은 global prior로 수축된 뒤에도 넓은 불확실성을 유지한다.
    variance = probability * (1 - probability) / max(1, gn + strength + 1)
    lcb = max(0.0, probability - 1.645 * math.sqrt(variance))
    return probability, lcb, gn


def oof_estimates(rows: list[dict], *, group_fields: tuple[str, ...] = ("kind", "pre_run_bucket", "score_bucket"),
                  folds: int = 4, embargo_days: int = 1, min_train: int = 30) -> list[dict]:
    """각 행을 미래에 노출되지 않은 학습표본으로만 확률화한다. 부족하면 명시적으로 abstain."""
    out: list[dict] = []
    for fold, (train_ids, test_ids) in enumerate(purged_folds(rows, folds=folds, embargo_days=embargo_days)):
        train = [rows[i] for i in train_ids]
        for i in test_ids:
            row = dict(rows[i], fold=fold)
            if len(train) < min_train:
                row.update({"probability": None, "lcb": None, "train_n": len(train), "group_train_n": 0,
                            "status": "abstain_insufficient_train"})
            else:
                p, lcb, gn = _estimate(train, row, group_fields)
                row.update({"probability": round(p, 4), "lcb": round(lcb, 4), "train_n": len(train),
                            "group_train_n": gn, "status": "shadow"})
            out.append(row)
    return out


def diagnostics(rows: list[dict], *, lcb_threshold: float = 0.5) -> dict:
    """OOF 품질 지표. 이 결과는 라이브 차단 조건이 아니라 사전등록/승격 여부 판단 재료다."""
    predicted = [r for r in rows if r.get("probability") is not None]
    selected = [r for r in predicted if r["lcb"] >= lcb_threshold]
    brier = (sum((r["probability"] - r["label"]) ** 2 for r in predicted) / len(predicted)
             if predicted else None)
    return {
        "labels": len(rows), "oof_predicted": len(predicted), "oof_abstained": len(rows) - len(predicted),
        "brier": round(brier, 4) if brier is not None else None,
        "lcb_threshold": lcb_threshold, "selected": len(selected),
        "selected_win_rate_pct": round(sum(r["label"] for r in selected) / len(selected) * 100, 1) if selected else None,
        "note": "shadow only — 사전등록 OOS 표본과 하한 기준 충족 전에는 매수·알림을 변경하지 않음",
    }
