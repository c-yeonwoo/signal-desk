"""Manual weight what-if is read-only and refuses incomplete price/cash inputs."""

import importlib
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient

from signal_desk import portfolio_weight_preview as preview


PROFILE = {"max_single_position_pct": 50, "max_sector_pct": 70, "min_cash_pct": 10}
ROWS = [
    {"ticker": "005930", "qty": 2, "close": 100, "close_date": "2026-10-08", "sector": "반도체"},
    {"ticker": "000660", "qty": 1, "close": 200, "close_date": "2026-10-08", "sector": "반도체"},
]


def _preview(**overrides):
    args = {"ticker": "005930", "amount": 100, "cash": 100, "rows": ROWS,
            "expected_session": "2026-10-08", "profile": PROFILE, "market": "kr"}
    args.update(overrides)
    return preview.preview(**args)


def test_weight_preview_only_moves_user_entered_cash_into_one_holding():
    result = _preview()
    assert result["ready"] is True
    assert result["total_value"] == 500
    assert result["weight_before_pct"] == 40
    assert result["weight_after_pct"] == 60
    assert result["cash_weight_before_pct"] == 20
    assert result["cash_weight_after_pct"] == 0
    assert result["sector_weight_before_pct"] == 80
    assert result["sector_weight_after_pct"] == 100
    assert result["limits"] == {"single_over": True, "sector_over": True, "cash_below_min": True}
    assert result["scope"] == "manual_analysis"
    assert result["not_order_advice"] is True and result["order_created"] is False


def test_preview_refuses_stale_missing_cross_market_or_unaffordable_inputs():
    assert not _preview(rows=[{**ROWS[0], "close_date": "2026-10-07"}, ROWS[1]])["ready"]
    assert not _preview(rows=[{**ROWS[0], "close": None}, ROWS[1]])["ready"]
    assert not _preview(rows=ROWS + [ROWS[0]])["ready"]
    assert not _preview(ticker="AAPL")["ready"]
    assert not _preview(amount=101)["ready"]
    assert not _preview(amount=float("nan"))["ready"]
    assert not _preview(cash=float("inf"))["ready"]
    assert not _preview(expected_session=None)["ready"]
    assert not _preview(rows=[])["ready"]


def test_unknown_sector_never_claims_a_sector_weight():
    result = _preview(rows=[ROWS[0], {**ROWS[1], "sector": None}])
    assert result["ready"] is True
    assert result["sector_weight_after_pct"] is None
    assert result["limits"]["sector_over"] is False


def test_api_is_private_market_scoped_and_does_not_create_snapshots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api as api_module
    importlib.reload(api_module)
    client = TestClient(api_module.app)
    assert client.post("/api/portfolio/weight-preview", json={"market": "kr", "ticker": "005930", "amount": 100}).status_code == 401
    client.post("/api/auth/signup", json={"email": "preview@example.com", "pw": "abcdef"})
    client.post("/api/holdings", json={"ticker": "005930", "qty": 2, "avg_price": 110})
    client.post("/api/holdings", json={"ticker": "AAPL", "qty": 10, "avg_price": 10})
    client.post("/api/portfolio/profile", json={"market": "kr", "cash": 100})
    monkeypatch.setattr(api_module.store, "load_portfolio_close_bundle", lambda market: (
        {"005930": [100], "AAPL": [10]}, {"005930": ["2026-10-08"], "AAPL": ["2026-10-08"]}))
    monkeypatch.setattr(api_module.store, "load_universe", lambda: [{"ticker": "005930", "sector": "반도체"}])
    monkeypatch.setattr(api_module.store, "load_us_universe", lambda: [{"ticker": "AAPL", "sector": "기술"}])
    monkeypatch.setattr(api_module.market_clock, "latest_completed_session", lambda market, now: "2026-10-08")
    response = client.post("/api/portfolio/weight-preview", json={"market": "kr", "ticker": "005930", "amount": 50})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["weight_before_pct"] == 66.67
    assert response.json()["weight_after_pct"] == 83.33
    assert response.json()["total_value"] == 300
    assert client.get("/api/portfolio/latest?market=kr").json()["ready"] is False
    assert client.post("/api/portfolio/weight-preview", json={"market": "invalid", "ticker": "005930", "amount": 10}).status_code == 400
    assert client.post("/api/portfolio/weight-preview", json={"market": "us", "ticker": "005930", "amount": 10}).json()["ready"] is False
    client.post("/api/auth/signup", json={"email": "other-preview@example.com", "pw": "abcdef"})
    assert client.post("/api/portfolio/weight-preview", json={"market": "kr", "ticker": "005930", "amount": 10}).json()["ready"] is False


def test_ui_preview_has_no_order_or_import_request_and_clears_on_market_change():
    html = (Path(__file__).resolve().parents[1] / "src/signal_desk/web/index.html").read_text()
    section = html.split("async function loadWeightPreview(", 1)[1].split("async function _liveRequest(", 1)[0]
    assert "/api/portfolio/weight-preview" in section
    assert "/api/live/" not in section and "/api/my-holdings/import" not in section
    assert "seq !== _weightPreviewSeq || market !== _pfMarket" in section
    assert "_clearWeightPreview();" in html.split("function setPfMarket(", 1)[1].split("async function refreshPortfolio(", 1)[0]
    if shutil.which("node"):
        result = subprocess.run(["node", "--check"], input=html.split("<script>", 1)[1].split("</script>", 1)[0],
                                text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
