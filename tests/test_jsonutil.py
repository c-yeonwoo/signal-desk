"""NaN/Inf JSON 안전화 — 시그널 리스트 500 방지."""

import json
import math

from signal_desk.api import SafeJSONResponse
from signal_desk.jsonutil import finite_or_none, json_safe
from signal_desk.signals import horizon


def test_finite_or_none():
    assert finite_or_none(1.5) == 1.5
    assert finite_or_none(float("nan")) is None
    assert finite_or_none(float("inf")) is None
    assert finite_or_none(None) is None
    assert finite_or_none(True) is None  # bool ≠ 숫자


def test_json_safe_nested_nan():
    out = json_safe({
        "a": float("nan"),
        "b": [1.0, float("inf"), {"c": float("nan")}],
        "ok": 2.5,
    })
    assert out == {"a": None, "b": [1.0, None, {"c": None}], "ok": 2.5}
    json.dumps(out, allow_nan=False)  # 예외 없어야 함


def test_safe_json_response_renders_nan():
    body = SafeJSONResponse({"x": float("nan"), "y": math.inf, "z": 1.0}).body
    assert json.loads(body) == {"x": None, "y": None, "z": 1.0}


def test_horizon_skips_nan_closes():
    closes = [100.0] * 61
    closes[-1] = float("nan")
    closes[0] = float("nan")
    rets = horizon.returns_at(closes)
    assert all(v is None for v in rets.values())
