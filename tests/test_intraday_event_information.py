import base64
import copy
import hashlib

import pytest

from scripts.measure import intraday_event_information as audit
from scripts.measure import intraday_orb_replay as orb


def frozen():
    return {"events": [{"id": "GOOGL:2025-02-04", "symbol": "GOOGL",
                        "reported_date": "2025-02-04", "fiscal_end": "2024-12-31"}]}


def news_page(tmp_path, request, *, token=None, rows=None):
    body = {"news": rows or [], "next_page_token": token}
    raw = orb.json_bytes(body)
    env = {"schema": "alpaca-historical-news-day-v1", "symbol": request["symbol"],
           "request": request["params"], "status": "observed" if rows else "empty",
           "raw_bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
           "raw_b64": base64.b64encode(raw).decode()}
    orb.write_new(tmp_path / (request["symbol"] + request["params"].get("page_token", "") + ".json"), env)


def test_alias_pagination_cannot_be_omitted_even_if_primary_has_a_positive_article(tmp_path):
    reqs = audit.news_requests(frozen())
    assert [r["symbol"] for r in reqs] == ["GOOGL", "GOOG"]
    news_page(tmp_path, reqs[0])
    with pytest.raises(ValueError, match="pagination incomplete"):
        audit.news_sources(tmp_path, frozen())
    news_page(tmp_path, reqs[1], token="next")
    with pytest.raises(ValueError, match="pagination incomplete"):
        audit.news_sources(tmp_path, frozen())
    last = copy.deepcopy(reqs[1]); last["params"]["page_token"] = "next"
    news_page(tmp_path, last)
    assert len(audit.news_sources(tmp_path, frozen())) == 3


def test_event_clock_ignores_misleading_premarket_label_and_uses_revision():
    e = {"status": "ready", "reported_date": "2025-02-06", "report_time_category": "pre-market",
         "ready_ns": orb.clock("2025-02-06T21:10:00Z")}
    assert audit.event_session(e, ["2025-02-06", "2025-02-07"]) == "2025-02-07"
    e["ready_ns"] = orb.clock("2025-02-06T14:35:00Z")
    assert audit.event_session(e, ["2025-02-06", "2025-02-07"]) == "2025-02-06"
    e["ready_ns"] += orb.SECOND
    assert audit.event_session(e, ["2025-02-06", "2025-02-07"]) == "2025-02-07"
    e["status"] = "news_missing"
    assert audit.event_session(e, ["2025-02-06", "2025-02-07"]) is None


def test_missing_news_is_not_a_profitable_cash_strategy():
    result = audit.assess(frozen(), [], {}, {}, {})
    assert result["verdict"] == "blocked_event_inputs"
    assert "path" not in result and not result["live_order_eligible"]


def test_source_cannot_be_replaced_with_a_different_event_day(tmp_path):
    reqs = audit.news_requests(frozen())
    reqs[0]["params"]["start"] = "2025-02-05T00:00:00-05:00"
    news_page(tmp_path, reqs[0])
    with pytest.raises(ValueError, match="outside frozen"):
        audit.news_sources(tmp_path, frozen())


def test_format_normalization_preserves_numbers_clocks_and_explicit_noncomparability():
    article = {"id": 1, "symbols": ["META"], "headline":
               "Meta Platforms Q4 2024 GAAP EPS $8.02 May Not Compare To $6.77 Estimate, Sales $48.39B Beat $47.03B Estimate",
               "created_at": "2025-01-29T21:42:18Z", "updated_at": "2025-01-29T21:42:19Z"}
    source = {"symbol": "META", "raw_sha256": "original", "body": {"news": [article]}}
    normalized, changes = audit.normalized_sources([source])
    derived = normalized[0]["body"]["news"][0]
    assert derived["headline"] == article["headline"].replace("Meta Platforms", "Meta").replace(" 2024 GAAP", "")
    assert {k: v for k, v in derived.items() if k != "headline"} == {k: v for k, v in article.items() if k != "headline"}
    assert "Meta Platforms" in source["body"]["news"][0]["headline"]
    assert changes[0]["gaap_label"]
    assert audit.events.old.EPS.search(derived["headline"]) is None
    # Revenue of an individual segment remains a segment, not issuer total.
    article["headline"] = "Meta Platforms Q4 2024 Advertising Sales $10B Beat $9B Estimate"
    normalized, _ = audit.normalized_sources([source])
    assert "Advertising Sales" in normalized[0]["body"]["news"][0]["headline"]


def test_reference_source_binding_rejects_modified_or_added_prices(tmp_path):
    path = tmp_path / "bars" / "AAPL" / "one.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original")
    mapping = {"bars/AAPL/one.json": hashlib.sha256(b"original").hexdigest()}
    reference = {"provenance": {"source_sha256": mapping}}
    assert audit.checked_intraday(tmp_path, reference) == hashlib.sha256(orb.json_bytes(mapping)).hexdigest()
    path.write_bytes(b"revision")
    with pytest.raises(ValueError, match="sources differ"):
        audit.checked_intraday(tmp_path, reference)
