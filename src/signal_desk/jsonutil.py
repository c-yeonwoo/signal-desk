"""JSON 직렬화 안전화 — NaN/Inf/numpy 스칼라가 응답을 깨지 않게.

표준 json은 NaN을 거절한다(allow_nan=False가 기본인 환경·Starlette JSONResponse).
시세·재무 parquet에서 빠진 값이 float('nan')으로 새면 /api/signals 전체가 500이 된다.
"""

from __future__ import annotations

import math
from typing import Any


def finite_or_none(value: Any) -> float | None:
    """숫자면 finite float, 아니면 None. bool은 제외(True→1.0 방지)."""
    if value is None or isinstance(value, bool):
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except Exception:
            pass
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def json_safe(obj: Any) -> Any:
    """응답 트리에서 NaN/Inf → None. numpy 스칼라는 Python 기본형으로."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            return json_safe(obj.item())
        except Exception:
            pass
    return obj
