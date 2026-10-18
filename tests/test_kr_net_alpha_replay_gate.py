"""Offline source gate; fixtures cannot be mistaken for real certification."""

import gzip
import hashlib
import json

import pytest

from scripts.measure import kr_net_alpha_replay as command
from test_research_net_alpha_replay import world
from test_research_net_alpha_v2 import reviewed


def fixture(tmp_path, monkeypatch):
    cfg, _ = world()
    monkeypatch.setattr(command.audit, "contract", lambda: {**cfg, "contract_hashes":{"fixture":"synthetic"}})
    monkeypatch.setattr(command.audit, "evidence", lambda path: reviewed())
    monkeypatch.setattr(command, "ROOT", tmp_path)
    monkeypatch.setattr(command, "DEST", tmp_path)
    roster = {"dates":[{"date":"2022-07-01"}]}
    raw = json.dumps(roster).encode(); (tmp_path/"universes.json").write_bytes(raw)
    roster_sha = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(command.audit, "ROSTER_SHA", roster_sha)
    packet = tmp_path/"packet.json"; packet.write_text("SYNTHETIC TEST ONLY")
    builders = {}
    for p in ("scripts/measure/kr_net_alpha_v2.py", "src/signal_desk/signals/research_net_alpha_v2.py",
              "src/signal_desk/signals/research_net_alpha.py", "src/signal_desk/signals/research_action_clocks.py"):
        path = tmp_path/p; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SYNTHETIC BUILDER")
        builders[p] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Result provenance also fingerprints the new files.
    for p in ("scripts/measure/kr_net_alpha_replay.py", "src/signal_desk/signals/research_net_alpha_account.py",
              "src/signal_desk/signals/research_net_alpha_execution.py", "src/signal_desk/signals/research_net_alpha_replay.py"):
        (tmp_path/p).write_text("SYNTHETIC REPLAY")
    body = {"schema":"kr-net-alpha-v2-readiness-v2", "candidate_id":cfg["candidate_id"],
        "contract_hashes":{"fixture":"synthetic"}, "roster_sha256":roster_sha,
        "evidence_packet_sha256":hashlib.sha256(packet.read_bytes()).hexdigest(),
        "implementation_sha256":builders, "input_gap_counts":{"synthetic_scope_missing":1},
        "phases":{"train":{"complete_label_dates":999}},
        "rows":[{"date":"2022-07-01", "snapshot":{"date":"2022-07-01", "state":"input_gap",
            "tickers":[]}, "sample":None, "labels":{}}]}
    return cfg, packet, body


def encode(tmp_path, body):
    path = tmp_path/"readiness.json.gz"
    raw = gzip.compress(json.dumps(body).encode())
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_recomputes_gate_does_not_use_claimed_counts_or_open_a_model(tmp_path, monkeypatch):
    cfg, packet, body = fixture(tmp_path, monkeypatch)
    path, sha = encode(tmp_path, body)
    _, report, model = command.preflight(path, sha, packet, tmp_path/"does-not-exist.json")
    assert model is None and report["state"] == "blocked_data"
    assert report["phases"]["train"]["complete_label_dates"] == 0
    assert report["paths_executed"] == 0 and report["expected_paths"] == 210
    assert report["net_return"] is None and not report["comparison_complete"]
    assert not report["live_order_eligible"] and not report["backtest_executed"]


def test_changed_builder_or_fabricated_sample_requires_new_audit(tmp_path, monkeypatch):
    cfg, packet, body = fixture(tmp_path, monkeypatch)
    path, sha = encode(tmp_path, body)
    builder = tmp_path/"src/signal_desk/signals/research_net_alpha_v2.py"
    builder.write_text("CHANGED BUILDER")
    with pytest.raises(ValueError, match="builder changed"):
        command.preflight(path, sha, packet)
    builder.write_text("SYNTHETIC BUILDER")
    body["rows"][0]["sample"] = {"returns":[100]}
    path, sha = encode(tmp_path, body)
    with pytest.raises(ValueError, match="sample admission"):
        command.preflight(path, sha, packet)
    with pytest.raises(ValueError, match="hash differs"):
        command.preflight(path, "0"*64, packet)
