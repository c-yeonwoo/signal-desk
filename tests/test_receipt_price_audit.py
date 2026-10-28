import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location("receipt_audit", Path(__file__).parents[1] / "scripts/measure/receipt_price_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_comparison_never_uses_receipt_minutes_future_close():
    import datetime as dt
    at = dt.datetime.fromisoformat("2026-10-26T14:40:10+00:00").timestamp()
    out = audit.compare([{"ticker": "APA", "received_at": at, "recorded_display_price": 101}], {
        "APA": [{"t": "2026-10-26T14:39:00Z", "c": 100}, {"t": "2026-10-26T14:40:00Z", "c": 120}]})
    assert out[0]["external_prior_minute_close"] == 100
    assert out[0]["difference_bps"] == 100
    assert "missing" in audit.compare([{"ticker": "APA", "received_at": at, "recorded_display_price": 101}], {})[0]["status"]
