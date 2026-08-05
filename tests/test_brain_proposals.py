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


# 2026-08-05 판정 게이트(N2): 정본 판정이 `판별력 있음` 으로 확정되기 전에는 **자동 제안을
# 만들지 않는다**. 아래 테스트들은 제안 생성·승인 로직 자체를 보는 것이므로 게이트를 열고 돈다.
# 게이트가 닫힌 동작은 `test_redteam.py` 의 N2 검사들이 본다 — 여기서 게이트를 끄는 것이
# "게이트가 없어도 통과"를 뜻하지 않게 그쪽에 검사를 따로 두었다.
def _open_gate(monkeypatch):
    monkeypatch.setattr(brain_proposals, "_verdict_gate", lambda *, automated: (True, ""))


# 2026-08-06 X1: 제안은 IC **스칼라**가 아니라 `accuracy.cross_sectional_ic` 산출물을 본다.
# 옛 계약은 `(ic, n)`이었고 n에 행 수(`matured_primary`)가 들어갔다 — 하루치 200종목이 표본 200이
# 되어 문턱을 즉시 통과했다. 이 헬퍼는 **유의한** 통계를 만든다(그래야 제안이 생성된다).
def _stat(ic, *, n_dates=30, p=0.001, significant=True, horizon=20):
    return {"ic": ic, "ic_mean": ic, "n_dates": n_dates, "n_pairs": n_dates * 190,
            "independent_dates": n_dates // horizon, "breadth_median": 190,
            "horizon": horizon, "ic_std": 0.05, "ic_ir": 1.0, "se": 0.02, "se_naive": 0.02,
            "ci95": 0.04, "t": 5.0, "p": p, "significant": significant,
            "nw_lag": horizon - 1, "nw_degenerate": False, "se_floored": False,
            "zero_variance": False, "min_dates": 20, "thin_dates": 0,
            "blocked_reason": None if significant else "IC가 0과 구분 불가 — 가중치 근거로 쓸 수 없음"}


def _acc(stats: dict, **extra) -> dict:
    return {"ready": True, "coverage": {"matured_primary": 45},
            "factor_ic": {k: v["ic"] for k, v in stats.items()},
            "factor_ic_stats": stats, "ic_min_dates": 20, **extra}


def test_automated_proposals_are_blocked_until_the_verdict_is_locked(tmp_path, monkeypatch):
    """게이트가 닫혀 있으면 제안이 **생성되지 않는다**(큐가 비고 이유가 남는다)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    acc = _acc({"short": _stat(-0.20), "momentum": _stat(0.20)})
    out = brain_proposals.refresh(acc, dict(_WEIGHTS))
    assert out["created"] == 0 and out.get("gated") is True
    assert "자동 제안" in out["reason"], out["reason"]
    assert db.brain_proposal_draft_count() == 0


def test_composite_ic_estimate_direction():
    weights = dict(_WEIGHTS)
    factor_ic = {"short": -0.10, "momentum": 0.10, "technical": 0.0}
    before = brain_proposals.composite_ic_estimate(factor_ic, weights)
    after_w = {**weights, "weight_short": weights["weight_short"] - 0.05}
    after = brain_proposals.composite_ic_estimate(factor_ic, after_w)
    assert before is not None and after is not None
    assert after > before  # 음수 IC 비중↓ → 추정 composite ↑


def test_build_nudge_negative_ic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = brain_proposals.build_weight_nudge("short", _stat(-0.05), _WEIGHTS)
    assert d is not None
    assert d["kind"] == "weight_nudge"
    assert "공매도" in d["title"]
    assert d["patch"]["weight_short"] == 0.10  # 0.15 - 0.05
    assert "잘 안 맞" in d["body_ko"]
    assert d["confidence"] in ("low", "medium", "high")
    # 근거 문구에 n·CI·p가 있어야 한다 — IC 크기만 쓰면 그게 판별력처럼 읽힌다.
    for token in ("IC", "±", "거래일", "독립", "p="):
        assert token in d["rationale_ko"], d["rationale_ko"]
    ev = d["evidence"]
    assert ev["n_dates"] == 30 and ev["significant"] is True and ev["p"] == 0.001


def test_build_boost_positive_ic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = brain_proposals.build_weight_boost("momentum", _stat(0.12), _WEIGHTS)
    assert d is not None
    assert "높이기" in d["title"]
    assert d["patch"]["weight_momentum"] == 0.25
    assert d["evidence"]["direction"] == "up"


def test_build_skips_timing_weak_and_insignificant(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert brain_proposals.build_weight_nudge("technical", _stat(-0.05), _WEIGHTS) is None
    assert brain_proposals.build_weight_boost("momentum", _stat(0.02), _WEIGHTS) is None  # IC 너무 작음
    # 유의하지 않은 IC로는 어느 방향도 제안하지 않는다 — 크기가 아니라 유의성이 게이트다.
    weak = _stat(-0.30, significant=False, p=0.42)
    assert brain_proposals.build_weight_nudge("short", weak, _WEIGHTS) is None
    assert brain_proposals.build_weight_boost("momentum", _stat(0.30, significant=False),
                                             _WEIGHTS) is None
    # 통계 자체가 없으면(옛 pooled 스칼라만 있는 응답) 아무것도 만들지 않는다.
    assert brain_proposals.build_weight_nudge("short", None, _WEIGHTS) is None
    assert brain_proposals.ic_usable(None)[0] is False


def test_refresh_names_why_each_factor_was_unusable(tmp_path, monkeypatch):
    """0의 이유는 변호가 아니라 점검 결과여야 한다 — 팩터 이름과 함께 낸다."""
    _open_gate(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db, "DB", tmp_path / "app.db")
    signalcfg.set_dict(_WEIGHTS)
    acc = _acc({"short": _stat(-0.20, significant=False, p=0.4),
                "momentum": {**_stat(0.20), "ic": None, "significant": False,
                             "n_dates": 10, "blocked_reason": "IC 날짜 10/20일 — 판정 불가"}})
    out = brain_proposals.refresh(acc, signalcfg.get_dict())
    assert out["created"] == 0
    assert any("공매도" in x for x in out["ic_skipped"]), out["ic_skipped"]
    assert any("10/20일" in x for x in out["ic_skipped"]), out["ic_skipped"]
    assert "안정권" not in out["reason"]


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
    _open_gate(monkeypatch)
    monkeypatch.chdir(tmp_path)
    signalcfg.set_dict(_WEIGHTS)
    acc = _acc({"short": _stat(-0.06), "momentum": _stat(0.10), "technical": _stat(-0.04)},
               buy_precision_pct=40.0, buy_sample=25)
    out = brain_proposals.refresh(acc, signalcfg.get_dict())
    assert out["ok"] and out["created"] >= 2
    drafts = db.brain_proposal_list(status="draft")
    assert any((d.get("evidence") or {}).get("factor") == "short" for d in drafts)
    assert any((d.get("evidence") or {}).get("direction") == "up" for d in drafts)
    assert any(d.get("kind") == "threshold_nudge" for d in drafts)
    assert not any((d.get("evidence") or {}).get("factor") == "technical" for d in drafts)

    short = next(d for d in drafts if (d.get("evidence") or {}).get("factor") == "short")
    ev = short.get("evidence") or {}
    assert ev.get("ab_kind") == "composite_ic"
    assert ev.get("before_composite_ic") is not None
    assert ev.get("after_composite_ic") is not None
    # 음수 IC 팩터 비중↓ → 추정 composite IC는 올라가야 함
    assert ev["after_composite_ic"] >= ev["before_composite_ic"]
    thr = next(d for d in drafts if d.get("kind") == "threshold_nudge")
    assert (thr.get("evidence") or {}).get("ab_kind") == "threshold_remeasure"

    before = signalcfg.get_dict()["weight_short"]
    rev = brain_proposals.review(short["id"], "approved", accuracy=acc)
    assert rev["ok"] and rev["status"] == "approved"
    assert signalcfg.get_dict()["weight_short"] < before
    hist = signalcfg.history()
    assert hist and hist[0]["source"] == "brain_proposal"
    snap = hist[0].get("accuracy_at_approve") or {}
    assert snap.get("buy_precision_pct") == 40.0
    assert snap.get("composite_ic") is not None
    assert snap.get("projected_composite_ic") is not None


def test_refresh_skips_immature_tracker(tmp_path, monkeypatch):
    _open_gate(monkeypatch)
    monkeypatch.chdir(tmp_path)
    out = brain_proposals.refresh({"ready": False}, _WEIGHTS)
    assert out["created"] == 0 and out.get("reason")
    assert "봇" in out["reason"] or "별개" in out["reason"]


def test_reject_leaves_config(tmp_path, monkeypatch):
    _open_gate(monkeypatch)
    monkeypatch.chdir(tmp_path)
    signalcfg.set_dict(_WEIGHTS)
    acc = _acc({"flow": _stat(-0.04)})
    brain_proposals.refresh(acc, signalcfg.get_dict())
    draft = db.brain_proposal_list(status="draft")[0]
    w0 = signalcfg.get_dict()["weight_flow"]
    assert brain_proposals.review(draft["id"], "rejected")["ok"]
    assert signalcfg.get_dict()["weight_flow"] == w0
    assert db.brain_proposal_get(draft["id"])["status"] == "rejected"


def test_refresh_idempotent_same_factor_draft(tmp_path, monkeypatch):
    _open_gate(monkeypatch)
    monkeypatch.chdir(tmp_path)
    signalcfg.set_dict(_WEIGHTS)
    acc = _acc({"quality": _stat(-0.07)})
    brain_proposals.refresh(acc, signalcfg.get_dict())
    brain_proposals.refresh(acc, signalcfg.get_dict())
    drafts = [d for d in db.brain_proposal_list(status="draft")
              if (d.get("evidence") or {}).get("factor") == "quality"]
    assert len(drafts) == 1


def test_double_approve_rejected(tmp_path, monkeypatch):
    _open_gate(monkeypatch)
    monkeypatch.chdir(tmp_path)
    signalcfg.set_dict(_WEIGHTS)
    acc = _acc({"short": _stat(-0.05)})
    brain_proposals.refresh(acc, signalcfg.get_dict())
    pid = db.brain_proposal_list(status="draft")[0]["id"]
    assert brain_proposals.review(pid, "approved")["ok"]
    w1 = signalcfg.get_dict()["weight_short"]
    again = brain_proposals.review(pid, "approved")
    assert again["ok"] is False
    assert signalcfg.get_dict()["weight_short"] == w1
