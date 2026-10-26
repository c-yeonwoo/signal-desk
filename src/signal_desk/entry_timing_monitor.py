"""Read-only, packaged research snapshot. Never acquire data or execute orders."""
from __future__ import annotations

import json
import math
from pathlib import Path

REPORT = Path(__file__).with_name("research_reports") / "entry_timing.json"


def snapshot() -> dict:
    if not REPORT.is_file():
        return {"status": "not_available", "research_only": True, "live_order_eligible": False}
    try:
        if REPORT.stat().st_size > 256_000:
            raise ValueError("report too large")
        value = json.loads(REPORT.read_bytes())
        def finite(item):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("nonfinite metric")
            if isinstance(item, dict):
                for child in item.values():
                    finite(child)
            if isinstance(item, list):
                for child in item:
                    finite(child)
        finite(value)
        if (value["schema"] != "entry-timing-comparison-v1" or value["live_order_eligible"] is not False
                or value["research_only"] is not True or len(value["paths"]) != 10):
            raise ValueError("incompatible research report")
        return {**value, "status": "available"}
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return {"status": "invalid_report", "research_only": True, "live_order_eligible": False}
