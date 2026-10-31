import asyncio
import copy
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from scripts.finance_tools import alpaca_readonly
from signal_desk import db
from signal_desk.signals import research_packet

NOW = dt.datetime.fromisoformat("2026-10-29T13:33:28+00:00")


def captured(**quote_changes):
    artifacts = {}

    def add(kind, data):
        identity = db.decision_artifact_estimate("us", kind, data)["id"]
        artifacts[kind] = {"id": identity, "payload": {
            "schema_version": 1, "market": "us", "kind": kind, "data": data}}
        return identity

    base = add("price_base", {"series": {"AAA": {"dates": ["2026-10-28"], "closes": [100.]}}})
    delta = add("quote_delta", {"price_base_id": base, "unclassified": {}, "quotes": {
        "AAA": {"price": 95., "received_at": NOW.timestamp() - 5,
                "source_timestamp": (NOW - dt.timedelta(seconds=10)).isoformat(),
                "source_timestamp_field": "timestamp", "source_time_verified": False,
                "observation_id": "observed-aaa", **quote_changes}}})
    engine = add("engine_input", {"today": "2026-10-29", "universe": [{"ticker": "AAA"}],
        "price_base_id": base, "quote_delta_id": delta, "price_structure_status": "verified",
        "signal_policy_id": "fixture-policy", "earnings_dates": {"AAA": "2026-10-30"}})
    gate = add("gate_input", {"engine_input_id": engine})
    output = add("signal_output", {"gate_input_id": gate, "universe_size": 1, "rows": [{
        "ticker": "AAA", "name": "fixture", "score": 1.8, "kind": "HOLD", "gates": ["trend"],
        "factor_scores": {"technical": .8, "momentum": 1},
        "score_contributions": {"technical": 0., "momentum": 1.2, "valuation": -.1}}]})
    return {"schema": "decision-price-bundle-v1", "market": "us",
            "signal_output_id": output, "artifacts": artifacts}


def rehash(source):
    """Keep a malformed fixture internally hash-consistent to test semantic checks."""
    artifacts = source["artifacts"]
    for kind, links in [("price_base", []), ("quote_delta", [("price_base_id", "price_base")]),
            ("engine_input", [("price_base_id", "price_base"), ("quote_delta_id", "quote_delta")]),
            ("gate_input", [("engine_input_id", "engine_input")]),
            ("signal_output", [("gate_input_id", "gate_input")])]:
        data = artifacts[kind]["payload"]["data"]
        for key, target in links:
            data[key] = artifacts[target]["id"]
        artifacts[kind]["id"] = db.decision_artifact_estimate("us", kind, data)["id"]
    source["signal_output_id"] = artifacts["signal_output"]["id"]
    return source


def test_packet_keeps_original_judgment_and_uses_actual_contribution_without_orders():
    source = captured()
    original = copy.deepcopy(source)
    out = research_packet.build(source, as_of=NOW)
    item = out["items"][0]
    assert source == original
    assert item["recorded_judgment"]["score"] == 1.8
    assert item["price"]["change_from_prior_close_pct"] == pytest.approx(-5.)
    assert item["thesis"]["supporting_score_contributions"] == {"momentum": 1.2}
    assert item["price"]["validity"]["eligible"]
    assert not item["price"]["validity"]["source_time_verified"]
    assert item["liquidity"]["status"] == "unknown"
    assert not out["profitability_verified"] and not out["live_order_eligible"]
    assert "account_cost_contract" in out["comparison_readiness"]["missing"]


def test_old_provider_time_is_not_certified_by_recent_receipt():
    source = captured(source_timestamp=(NOW - dt.timedelta(seconds=601)).isoformat())
    item = research_packet.build(source, as_of=NOW)["items"][0]
    assert not item["price"]["validity"]["eligible"]
    assert item["price"]["validity"]["reason"] == "deferred_source_time"
    assert item["recorded_judgment"]["kind"] == "HOLD"


def test_future_receipt_is_not_used_as_past_research_evidence():
    with pytest.raises(ValueError, match="after research cutoff"):
        research_packet.build(captured(received_at=NOW.timestamp() + 1), as_of=NOW)


@pytest.mark.parametrize("kind", ["dated_bar", "quote"])
def test_future_nonuniverse_context_is_also_rejected(kind):
    source = captured()
    source["artifacts"]["price_base"]["payload"]["data"]["series"]["CONTEXT"] = {
        "closes": [100.], "dates": ["2026-10-30" if kind == "dated_bar" else "2026-10-28"]}
    if kind == "quote":
        source["artifacts"]["quote_delta"]["payload"]["data"]["quotes"]["CONTEXT"] = {
            "price": 100., "received_at": NOW.timestamp() + 1}
    with pytest.raises(ValueError):
        research_packet.build(rehash(source), as_of=NOW)


@pytest.mark.parametrize("as_of", [NOW.replace(tzinfo=None), NOW + dt.timedelta(days=1)])
def test_analysis_cutoff_must_be_explicit_aware_and_same_market_date(as_of):
    with pytest.raises(ValueError):
        research_packet.build(captured(), as_of=as_of)


def test_changed_artifact_content_cannot_enter_skill_context():
    source = captured()
    source["artifacts"]["quote_delta"]["payload"]["data"]["quotes"]["AAA"]["price"] = 999.
    with pytest.raises(ValueError, match="hash mismatch"):
        research_packet.build(source, as_of=NOW)


def test_launcher_passes_only_data_credentials_and_forces_official_endpoint(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('APCA_API_KEY_ID="fixture-key"\nAPCA_API_SECRET_KEY=fixture-secret\nANTHROPIC_API_KEY=not-passed\n')
    out = alpaca_readonly.environment(env_file, {"PATH": "/bin", "HOME": "/tmp",
        "ALPACA_TOOLSETS": "trading", "ALPACA_PAPER_TRADE": "false",
        "DATA_API_URL": "https://wrong.invalid", "HTTPS_PROXY": "https://wrong.invalid",
        "KIS_APP_KEY": "not-passed"})
    assert out["ALPACA_API_KEY"] == "fixture-key" and out["ALPACA_SECRET_KEY"] == "fixture-secret"
    assert out["ALPACA_TOOLSETS"] == "stock-data" and out["ALPACA_PAPER_TRADE"] == "true"
    assert out["DATA_API_URL"] == "https://data.alpaca.markets"
    assert not {"KIS_APP_KEY", "ANTHROPIC_API_KEY", "HTTPS_PROXY", "APCA_API_KEY_ID"} & out.keys()


def test_conflicting_credential_aliases_stop_before_server_creation(tmp_path):
    p = tmp_path / ".env"
    p.write_text('APCA_API_KEY_ID=one\nAPCA_API_SECRET_KEY=secret\nALPACA_API_KEY=two\n')
    with pytest.raises(ValueError, match="conflicting"):
        alpaca_readonly.environment(p, {})


def test_unreviewed_server_upgrade_is_not_silently_accepted(monkeypatch):
    origin = {"url": "https://github.com/alpacahq/alpaca-mcp-server", "vcs_info": {"commit_id": "different"}}
    monkeypatch.setattr(alpaca_readonly.metadata, "distribution", lambda _: SimpleNamespace(
        version="2.4.1", read_text=lambda _: json.dumps(origin)))
    with pytest.raises(ValueError, match="reviewed"):
        alpaca_readonly.verify_install()


@pytest.mark.parametrize("unexpected", ["place_stock_order", "get_crypto_bars", "get_market_movers"])
def test_unexpected_tool_registration_fails_closed(unexpected):
    class Server:
        def enable(self, **kwargs):
            assert kwargs["only"] and kwargs["names"] == alpaca_readonly.ALLOWED

        async def list_tools(self):
            return [SimpleNamespace(name=n) for n in alpaca_readonly.ALLOWED | {unexpected}]

    with pytest.raises(ValueError, match="inventory"):
        asyncio.run(alpaca_readonly.restrict(Server()))
