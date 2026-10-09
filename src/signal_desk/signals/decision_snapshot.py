"""P2 가격 입력 보존: 일봉 기준본과 실제 사용한 장중 변경분을 분리한다.

이 모듈은 주문 자격을 결정하지 않는다. 날짜가 없는 가격을 임의의 종가로 만들지 않고,
정확한 수치 재생과 원천 시점의 검증 여부를 별개로 다룬다.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import asdict, is_dataclass
from zoneinfo import ZoneInfo

from signal_desk import db

_QUOTE_META_KEYS = ("observation_id", "provider", "price_kind", "currency",
                    "source_timestamp", "source_timestamp_field", "source_time_verified")


def _prices(values: list, *, ticker: str) -> list[float]:
    if not isinstance(values, list):
        raise ValueError(f"{ticker}: prices must be a list")
    out = []
    for value in values:
        try:
            price = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{ticker}: invalid price") from exc
        if not math.isfinite(price):
            raise ValueError(f"{ticker}: nonfinite price")
        out.append(price)
    return out


def split_price_inputs(prices: dict[str, list[float]], dates: dict[str, list[str]],
                       quote_snapshot: dict) -> tuple[dict, dict, list[dict]]:
    """엔진 가격 배열을 기준본·장중 변경분으로 나눈다. 미분류 꼬리는 숨기지 않는다."""
    if not isinstance(prices, dict) or not isinstance(dates, dict) or not isinstance(quote_snapshot, dict):
        raise ValueError("invalid price input bundle")
    base = {"legacy_source_time_verified": False, "series": {}}
    delta = {"quotes": {}, "unclassified": {}}
    issues = []
    quotes = quote_snapshot.get("quotes") or {}
    received = quote_snapshot.get("quote_updated") or {}
    meta_by = quote_snapshot.get("quote_meta") or {}
    captured_at = quote_snapshot.get("captured_at")
    for ticker in sorted(set(prices) | set(dates)):
        if not isinstance(ticker, str) or not ticker:
            raise ValueError("invalid ticker")
        closes = _prices(prices.get(ticker) or [], ticker=ticker)
        sessions = dates.get(ticker) or []
        if not isinstance(sessions, list) or not all(isinstance(day, str) for day in sessions):
            raise ValueError(f"{ticker}: invalid dates")
        if sessions != sorted(set(sessions)):
            issues.append({"ticker": ticker, "reason": "dates_not_unique_sorted"})
        n_base = min(len(closes), len(sessions))
        base["series"][ticker] = {"dates": list(sessions), "closes": closes[:n_base]}
        if len(sessions) > len(closes):
            issues.append({"ticker": ticker, "reason": "price_missing_for_date"})
        tail = closes[n_base:]
        if not tail:
            continue
        try:
            quote_price = float(quotes.get(ticker))
            quote_received = float(received.get(ticker))
            age = float(captured_at) - quote_received
            quote_matches = (len(tail) == 1 and math.isfinite(quote_price)
                             and quote_price == tail[0] and -300 <= age <= 600)
        except (TypeError, ValueError):
            quote_matches = False
        if quote_matches:
            meta = meta_by.get(ticker) or {}
            delta["quotes"][ticker] = {
                "price": tail[0], "received_at": quote_received,
                **{key: meta.get(key) for key in _QUOTE_META_KEYS},
            }
            if not meta.get("observation_id"):
                issues.append({"ticker": ticker, "reason": "observation_id_missing"})
        else:
            delta["unclassified"][ticker] = tail
            issues.append({"ticker": ticker, "reason": "undated_price_not_matching_quote"})
    return base, delta, issues


def persist_price_inputs(market: str, prices: dict[str, list[float]],
                         dates: dict[str, list[str]], quote_snapshot: dict, *,
                         observed_at: int | None = None) -> dict:
    """가격 원장에 두 조각만 기록한다. 라이브 계산 경로에서는 아직 자동 호출하지 않는다."""
    base, delta, issues = split_price_inputs(prices, dates, quote_snapshot)
    base_id = db.decision_artifact_put(market, "price_base", base, observed_at=observed_at)
    delta_id = db.decision_artifact_put(market, "quote_delta",
                                        {"price_base_id": base_id, **delta},
                                        observed_at=observed_at)
    return {"price_base_id": base_id, "quote_delta_id": delta_id,
            "structure_status": "verified" if not issues else "partial", "issues": issues,
            "strict_pit_eligible": False}


def load_price_inputs(market: str, price_base_id: str, quote_delta_id: str) -> tuple[dict, dict]:
    """내용 해시와 참조를 검증하고 원래 엔진 가격 배열·원본 날짜를 복원한다."""
    base = db.decision_artifact_get(price_base_id)
    delta = db.decision_artifact_get(quote_delta_id)
    if not base or not delta or base["market"] != market or delta["market"] != market:
        raise ValueError("price artifact missing or market mismatch")
    if base["kind"] != "price_base" or delta["kind"] != "quote_delta":
        raise ValueError("price artifact kind mismatch")
    if delta["data"].get("price_base_id") != price_base_id:
        raise ValueError("quote delta references another price base")
    series = base["data"].get("series") or {}
    quotes = delta["data"].get("quotes") or {}
    unclassified = delta["data"].get("unclassified") or {}
    if set(quotes) & set(unclassified) or (set(quotes) | set(unclassified)) - set(series):
        raise ValueError("invalid quote delta tickers")
    prices = {}
    dates = {}
    for ticker, item in series.items():
        tail = ([quotes[ticker]["price"]] if ticker in quotes else unclassified.get(ticker, []))
        prices[ticker] = list(item["closes"]) + list(tail)
        dates[ticker] = list(item["dates"])
    return prices, dates


def _plain(value):
    """엔진 입력의 지원 타입만 명시적으로 JSON 값으로 바꾼다. 임의 객체 repr은 금지."""
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("decision input keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"unsupported decision input type: {type(value).__name__}")


def _prepare_signal_decision(market: str, *, prices: dict[str, list[float]],
                             dates: dict[str, list[str]], quote_snapshot: dict,
                             engine_inputs: dict, gate_inputs: dict, results: list) -> tuple:
    """실제 저장할 다섯 조각을 쓰기 없이 만들고 압축 크기를 계산한다."""
    from signal_desk.signals import engine, execution_gate

    if (not isinstance(engine_inputs.get("today"), datetime.date)
            or isinstance(engine_inputs.get("today"), datetime.datetime)):
        raise ValueError("engine today must be the exact date used by evaluate")
    if not gate_inputs.get("today"):
        raise ValueError("gate today must be explicit")
    gate_status = gate_inputs.get("status", "applied")
    if gate_status not in ("applied", "empty") or (gate_status == "empty" and results):
        raise ValueError("failed or inconsistent gate capture cannot be replayed")
    cfg = engine_inputs.get("config") or engine.SignalConfig()
    gate_cfg = gate_inputs.get("config") or execution_gate.ExecutionGateConfig()
    # 지원하지 않는 원천 타입/비유한 수치는 어떤 가격 조각도 쓰기 전에 거절한다.
    engine_core = _plain({
        "universe": engine_inputs["universe"],
        "fundamentals": engine_inputs.get("fundamentals") or {},
        "sentiment": engine_inputs.get("sentiment") or {},
        "flows": engine_inputs.get("flows") or {},
        "shorts": engine_inputs.get("shorts") or {},
        "earnings_dates": engine_inputs.get("earnings_dates") or {},
        "unavailable": engine_inputs.get("unavailable") or (),
        "config": cfg,
        "today": engine_inputs["today"],
        "signal_policy_id": engine_inputs.get("signal_policy_id"),
    })
    gate_core = _plain({
        "status": gate_status,
        "hist_by": gate_inputs.get("hist_by") or {},
        "events_by": gate_inputs.get("events_by") or {},
        "today": gate_inputs["today"],
        "config": gate_cfg,
    })
    output_core = _plain({
        "universe_size": len(engine_inputs["universe"]),
        "rows": [asdict(result) for result in results],
    })
    base, delta, issues = split_price_inputs(prices, dates, quote_snapshot)
    prepared = []

    def add(kind: str, payload: dict) -> str:
        estimate = db.decision_artifact_estimate(market, kind, payload)
        prepared.append((kind, payload, estimate))
        return estimate["id"]

    base_id = add("price_base", base)
    delta_id = add("quote_delta", {"price_base_id": base_id, **delta})
    price_refs = {"price_base_id": base_id, "quote_delta_id": delta_id,
                  "structure_status": "verified" if not issues else "partial", "issues": issues,
                  "strict_pit_eligible": False}
    engine_payload = {
        "price_base_id": base_id,
        "quote_delta_id": delta_id,
        "price_structure_status": price_refs["structure_status"],
        "price_issues": price_refs["issues"],
        **engine_core,
    }
    engine_id = add("engine_input", engine_payload)
    gate_payload = {"engine_input_id": engine_id, **gate_core}
    gate_id = add("gate_input", gate_payload)
    output_payload = {"gate_input_id": gate_id, **output_core}
    output_id = add("signal_output", output_payload)
    refs = {**price_refs, "engine_input_id": engine_id, "gate_input_id": gate_id,
            "signal_output_id": output_id, "replay_attemptable": True}
    size = {"raw_bytes": sum(row[2]["raw_bytes"] for row in prepared),
            "stored_bytes": sum(row[2]["stored_bytes"] for row in prepared)}
    return prepared, refs, size


def persist_signal_decision(market: str, *, prices: dict[str, list[float]],
                            dates: dict[str, list[str]], quote_snapshot: dict,
                            engine_inputs: dict, gate_inputs: dict, results: list,
                            observed_at: int | None = None) -> dict:
    """실제로 사용한 값만 묶어 보존한다. 호출자가 읽지 않은 원천을 추정해 채우지 않는다."""
    prepared, refs, _size = _prepare_signal_decision(
        market, prices=prices, dates=dates, quote_snapshot=quote_snapshot,
        engine_inputs=engine_inputs, gate_inputs=gate_inputs, results=results)
    for kind, payload, estimate in prepared:
        saved_id = db.decision_artifact_put(market, kind, payload, observed_at=observed_at)
        if saved_id != estimate["id"]:
            raise RuntimeError("decision artifact changed between estimation and storage")
    return refs


def _validate_capture(price_bundle: tuple[dict, dict, dict], capture: dict) -> None:
    prices, dates, _quote_snapshot = price_bundle
    gate = capture.get("gate_inputs") or {}
    if gate.get("status") not in ("applied", "empty"):
        raise ValueError("gate inputs were not captured successfully")
    if gate.get("closes_by") != prices or gate.get("dates_by") != dates:
        raise ValueError("gate price generation differs from engine prices")


def estimate_captured_decision(market: str, price_bundle: tuple[dict, dict, dict],
                               capture: dict) -> dict:
    """복제된 게이트 가격을 빼고 실제 저장 조각의 압축 상한을 재는 읽기 전용 경로."""
    _validate_capture(price_bundle, capture)
    prices, dates, quote_snapshot = price_bundle
    _prepared, _refs, size = _prepare_signal_decision(
        market, prices=prices, dates=dates, quote_snapshot=quote_snapshot,
        engine_inputs=capture["engine_inputs"], gate_inputs=capture["gate_inputs"],
        results=capture["results"])
    return size


def persist_captured_decision(market: str, price_bundle: tuple[dict, dict, dict],
                              capture: dict, *, observed_at: int | None = None) -> dict:
    """실제 계산 경로가 남긴 인자만 저장한다. 가격 재조회나 실패 게이트의 추정은 금지."""
    _validate_capture(price_bundle, capture)
    prices, dates, quote_snapshot = price_bundle
    return persist_signal_decision(
        market, prices=prices, dates=dates, quote_snapshot=quote_snapshot,
        engine_inputs=capture["engine_inputs"], gate_inputs=capture["gate_inputs"],
        results=capture["results"], observed_at=observed_at,
    )


def replay_signal_decision(market: str, signal_output_id: str) -> dict:
    """저장된 값으로 전체 횡단면·게이트를 다시 계산한다. 주문/연구 look은 실행하지 않는다."""
    from signal_desk.signals import engine, execution_gate, reversion

    output = db.decision_artifact_get(signal_output_id)
    if not output or output["kind"] != "signal_output" or output["market"] != market:
        raise ValueError("signal output missing or market mismatch")
    expected = output["data"]
    gate_id = expected["gate_input_id"]
    gate = db.decision_artifact_get(gate_id)
    if not gate or gate["kind"] != "gate_input" or gate["market"] != market:
        raise ValueError("gate input missing or market mismatch")
    engine_id = gate["data"]["engine_input_id"]
    inputs = db.decision_artifact_get(engine_id)
    if not inputs or inputs["kind"] != "engine_input" or inputs["market"] != market:
        raise ValueError("engine input missing or market mismatch")
    saved = inputs["data"]
    prices, dates = load_price_inputs(market, saved["price_base_id"], saved["quote_delta_id"])
    cfg_data = dict(saved["config"])
    cfg_data["reversion"] = reversion.ReversionConfig(**cfg_data["reversion"])
    cfg_data["backtest_horizons"] = tuple(cfg_data["backtest_horizons"])
    cfg = engine.SignalConfig(**cfg_data)
    results = engine.evaluate(
        saved["universe"], prices, fundamentals=saved["fundamentals"], config=cfg,
        sentiment=saved["sentiment"], flows=saved["flows"], shorts=saved["shorts"],
        earnings_dates=saved["earnings_dates"], unavailable=tuple(saved["unavailable"]),
        today=datetime.date.fromisoformat(saved["today"]),
    )
    for result in results:
        result.signal_policy_id = saved.get("signal_policy_id")
    gate_data = gate["data"]
    if gate_data["status"] == "applied":
        execution_gate.apply(
            results, hist_by=gate_data["hist_by"], dates_by=dates, closes_by=prices,
            events_by=gate_data["events_by"], today=gate_data["today"],
            cfg=execution_gate.ExecutionGateConfig(**gate_data["config"]),
        )
    actual_rows = _plain([asdict(result) for result in results])
    expected_rows = expected["rows"]
    mismatched = []
    for index in range(max(len(actual_rows), len(expected_rows))):
        actual = actual_rows[index] if index < len(actual_rows) else None
        original = expected_rows[index] if index < len(expected_rows) else None
        if actual != original:
            ticker = (original or actual or {}).get("ticker")
            mismatched.append(ticker or f"row-{index}")
    return {"match": not mismatched and len(saved["universe"]) == expected["universe_size"],
            "market": market, "signal_output_id": signal_output_id,
            "universe_size": len(saved["universe"]), "expected_rows": len(expected_rows),
            "replayed_rows": len(actual_rows), "mismatched_tickers": mismatched[:20],
            "price_structure_status": saved["price_structure_status"],
            "strict_pit_eligible": False}


def audit_replay_timing(market: str, signal_output_id: str) -> dict:
    """Read-only check of the recorded input boundary, not source-availability proof.

    An exact current-code replay can still contain future bars. Keep these claims apart.
    """
    output = db.decision_artifact_get(signal_output_id)
    if not output or output["market"] != market or output["kind"] != "signal_output":
        raise ValueError("signal output missing or market mismatch")
    gate = db.decision_artifact_get(output["data"]["gate_input_id"])
    if not gate or gate["market"] != market or gate["kind"] != "gate_input":
        raise ValueError("gate input missing or market mismatch")
    engine_input = db.decision_artifact_get(gate["data"]["engine_input_id"])
    if not engine_input or engine_input["market"] != market or engine_input["kind"] != "engine_input":
        raise ValueError("engine input missing or market mismatch")
    saved = engine_input["data"]
    base = db.decision_artifact_get(saved["price_base_id"])
    delta = db.decision_artifact_get(saved["quote_delta_id"])
    if not base or not delta or base["market"] != market or delta["market"] != market:
        raise ValueError("price artifact missing or market mismatch")
    if base["kind"] != "price_base" or delta["kind"] != "quote_delta":
        raise ValueError("price artifact kind mismatch")
    if delta["data"].get("price_base_id") != saved["price_base_id"]:
        raise ValueError("quote delta references another price base")
    day = datetime.date.fromisoformat(saved["today"])
    bad_dates = []
    for ticker, series in base["data"]["series"].items():
        if any(datetime.date.fromisoformat(value) > day for value in series["dates"]):
            bad_dates.append(ticker)
    zone = ZoneInfo("Asia/Seoul" if market == "kr" else "America/New_York")
    late_quotes = []
    for ticker, quote in delta["data"].get("quotes", {}).items():
        received_day = datetime.datetime.fromtimestamp(float(quote["received_at"]), zone).date()
        if received_day > day:
            late_quotes.append(ticker)
    undated = sorted(delta["data"].get("unclassified", {}))
    gate_day = str(gate["data"]["today"])[:10]
    issues = []
    if gate_day != day.isoformat():
        issues.append("engine_gate_day_mismatch")
    if bad_dates:
        issues.append("future_dated_bars")
    if late_quotes:
        issues.append("future_received_quotes")
    if undated:
        issues.append("undated_price_tails")
    issues.extend(str(item.get("reason")) for item in saved.get("price_issues", []))
    return {
        "engine_day": day.isoformat(), "gate_day": gate_day,
        "universe_size": len(saved["universe"]),
        "priced_tickers": len(base["data"]["series"]),
        "future_dated_bar_tickers": sorted(bad_dates),
        "future_received_quote_tickers": sorted(late_quotes),
        "undated_price_tickers": undated,
        "structural_timing_issues": sorted(set(issues)),
        "structural_timing_clear": not issues,
        "source_available_at_verified": False,
        "strict_pit_eligible": False,
    }
