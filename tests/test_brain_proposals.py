"""두뇌 개선 제안 — IC 기반 draft 생성·승인/반려·설정 이력."""

from signal_desk import brain_proposals, db, signalcfg


_WEIGHTS = {
    "weight_technical": 0.35, "weight_fundamental": 0.30, "weight_valuation": 0.15,
    "weight_reversion": 0.20, "weight_flow": 0.20, "weight_quality": 0.15,
    "weight_momentum": 0.20, "weight_short": 0.15,
    "strong_buy_threshold": 2.0, "buy_threshold": 1.2,
    "sell_threshold": -0.3, "strong_sell_threshold": -0.6,
    "regime_adaptive": 1.0,
}


def test_build_nudge_negative_ic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = brain_proposals.build_weight_nudge("short", -0.05, 40, _WEIGHTS)
    assert d is not None
    assert d["kind"] == "weight_nudge"
    assert "공매도" in d["title"]
    assert d["patch"]["weight_short"] == 0.10  # 0.15 - 0.05
    assert "잘 안 맞" in d["body_ko"]
    assert d["confidence"] in ("low", "medium", "high")


def test_build_boost_positive_ic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = brain_proposals.build_weight_boost("momentum", 0.12, 40, _WEIGHTS)
    assert d is not None
    assert "높이기" in d["title"]
    assert d["patch"]["weight_momentum"] == 0.25
    assert d["evidence"]["direction"] == "up"


def test_build_skips_timing_and_weak(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert brain_proposals.build_weight_nudge("technical", -0.05, 40, _WEIGHTS) is None
    assert brain_proposals.build_weight_boost("momentum", 0.02, 40, _WEIGHTS) is None  # IC 너무 작음
    assert brain_proposals.build_weight_nudge("short", -0.05, 5, _WEIGHTS) is None


def test_threshold_nudge_low_and_high_precision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    low = brain_proposals.build_threshold_nudge(
        {"buy_precision_pct": 40.0, "buy_sample": 30,
         "coverage": {"matured_primary": 40}}, _WEIGHTS)
    assert low and low["patch"]["buy_threshold"] == 1.3
    assert "높이기" in low["title"]
    high = brain_proposals.build_threshold_nudge(
        {"buy_precision_pct": 62.0, "buy_sample": 30,
         "coverage": {"matured_primary": 40}}, _WEIGHTS)
    assert high and high["patch"]["buy_threshold"] == 1.1
    assert "낮추기" in high["title"]
    mid = brain_proposals.build_threshold_nudge(
        {"buy_precision_pct": 50.0, "buy_sample": 30,
         "coverage": {"matured_primary": 40}}, _WEIGHTS)
    assert mid is None


def test_refresh_creates_down_up_and_approve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    signalcfg.set_dict(_WEIGHTS)
    acc = {
        "ready": True,
        "factor_ic": {"short": -0.06, "momentum": 0.10, "technical": -0.04},
        "coverage": {"matured_primary": 45},
        "buy_precision_pct": 40.0,
        "buy_sample": 25,
    }
    out = brain_proposals.refresh(acc, signalcfg.get_dict())
    assert out["ok"] and out["created"] >= 2
    drafts = db.brain_proposal_list(status="draft")
    assert any((d.get("evidence") or {}).get("factor") == "short" for d in drafts)
    assert any((d.get("evidence") or {}).get("direction") == "up" for d in drafts)
    assert any(d.get("kind") == "threshold_nudge" for d in drafts)
    assert not any((d.get("evidence") or {}).get("factor") == "technical" for d in drafts)

    short = next(d for d in drafts if (d.get("evidence") or {}).get("factor") == "short")
    before = signalcfg.get_dict()["weight_short"]
    rev = brain_proposals.review(short["id"], "approved")
    assert rev["ok"] and rev["status"] == "approved"
    assert signalcfg.get_dict()["weight_short"] < before
    hist = signalcfg.history()
    assert hist and hist[0]["source"] == "brain_proposal"


def test_refresh_skips_immature_tracker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = brain_proposals.refresh({"ready": False}, _WEIGHTS)
    assert out["created"] == 0 and out.get("reason")
    assert "봇" in out["reason"] or "별개" in out["reason"]


def test_reject_leaves_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    signalcfg.set_dict(_WEIGHTS)
    acc = {"ready": True, "factor_ic": {"flow": -0.04}, "coverage": {"matured_primary": 30}}
    brain_proposals.refresh(acc, signalcfg.get_dict())
    draft = db.brain_proposal_list(status="draft")[0]
    w0 = signalcfg.get_dict()["weight_flow"]
    assert brain_proposals.review(draft["id"], "rejected")["ok"]
    assert signalcfg.get_dict()["weight_flow"] == w0
    assert db.brain_proposal_get(draft["id"])["status"] == "rejected"


def test_refresh_idempotent_same_factor_draft(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    signalcfg.set_dict(_WEIGHTS)
    acc = {"ready": True, "factor_ic": {"quality": -0.07}, "coverage": {"matured_primary": 50}}
    brain_proposals.refresh(acc, signalcfg.get_dict())
    brain_proposals.refresh(acc, signalcfg.get_dict())
    drafts = [d for d in db.brain_proposal_list(status="draft")
              if (d.get("evidence") or {}).get("factor") == "quality"]
    assert len(drafts) == 1


def test_double_approve_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    signalcfg.set_dict(_WEIGHTS)
    acc = {"ready": True, "factor_ic": {"short": -0.05}, "coverage": {"matured_primary": 40}}
    brain_proposals.refresh(acc, signalcfg.get_dict())
    pid = db.brain_proposal_list(status="draft")[0]["id"]
    assert brain_proposals.review(pid, "approved")["ok"]
    w1 = signalcfg.get_dict()["weight_short"]
    again = brain_proposals.review(pid, "approved")
    assert again["ok"] is False
    assert signalcfg.get_dict()["weight_short"] == w1
