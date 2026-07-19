"""시그널 엔진 설정(팩터 가중치·임계값) 영속화 — 관리자가 조정하면 시그널·백테스트에 반영.

기본값은 engine.SignalConfig, 관리자 오버라이드는 db.kv('signal_config')에 저장한다.
api._signals / _backtest / bot이 모두 이 get_config()를 써서 동일 설정으로 계산한다.
"""

from __future__ import annotations

import time
from dataclasses import replace

from signal_desk import db
from signal_desk.signals import regime as regime_mod
from signal_desk.signals.engine import SignalConfig

_KEY = "signal_config"
# 관리자 조정 대상(점수에 실제 들어가는 팩터만 — KB 정성은 veto/shadow라 가중치 UI 제외)
FIELDS = ["weight_technical", "weight_fundamental", "weight_valuation",
          "weight_reversion", "weight_flow", "weight_quality", "weight_momentum",
          "weight_short",
          "strong_buy_threshold", "buy_threshold", "sell_threshold", "strong_sell_threshold",
          "regime_adaptive"]

# P3: 정성 승격 모드 — combine()과 분리. 이번 PR은 off|shadow만.
_QUAL_KEY = "qualitative_promotion"
_QUAL_HIST_KEY = "qualitative_promotion_history"
_QUAL_HIST_MAX = 30
QUALITATIVE_MODES = ("off", "shadow")


def get_config() -> SignalConfig:
    """저장된 오버라이드를 얹은 SignalConfig. 없으면 기본값."""
    cfg = SignalConfig()
    ov = db.kv_get(_KEY) or {}
    for f in FIELDS:
        if f in ov and ov[f] is not None:
            setattr(cfg, f, float(ov[f]))
    return cfg


def get_dict() -> dict:
    cfg = get_config()
    return {f: getattr(cfg, f) for f in FIELDS}


_HISTORY_KEY = "signal_config_history"
_HISTORY_MAX = 50


def set_dict(data: dict) -> dict:
    """관리자 입력 저장(허용 필드만, 숫자 검증). 저장된 dict 반환."""
    ov = {}
    for f in FIELDS:
        if f in data and data[f] is not None:
            ov[f] = round(float(data[f]), 3)
    db.kv_set(_KEY, ov)
    return get_dict()


def reset() -> dict:
    db.kv_set(_KEY, {})
    return get_dict()


def append_history(entry: dict) -> None:
    """설정 변경 감사 로그(최신 앞·최대 50건). entry: {ts, source, before, after, ...}."""
    hist = db.kv_get(_HISTORY_KEY) or []
    if not isinstance(hist, list):
        hist = []
    hist.insert(0, entry)
    db.kv_set(_HISTORY_KEY, hist[:_HISTORY_MAX])


def history(limit: int = 20) -> list[dict]:
    hist = db.kv_get(_HISTORY_KEY) or []
    if not isinstance(hist, list):
        return []
    return hist[:limit]


def get_qualitative_mode() -> dict:
    """정성 승격 모드. 기본 off. priority/threshold는 아직 저장 불가."""
    raw = db.kv_get(_QUAL_KEY) or {}
    mode = raw.get("mode") if isinstance(raw, dict) else None
    if mode not in QUALITATIVE_MODES:
        mode = "off"
    return {
        "mode": mode,
        "updated": raw.get("updated") if isinstance(raw, dict) else None,
        "approved_by": (raw.get("approved_by") or "") if isinstance(raw, dict) else "",
        "note": (raw.get("note") or "") if isinstance(raw, dict) else "",
    }


def set_qualitative_mode(mode: str, *, approved_by: str = "", note: str = "",
                         gates_snapshot: dict | None = None) -> dict:
    """off|shadow만 허용. 변경마다 감사 이력 기록. combine/가중치와 무관."""
    if mode not in QUALITATIVE_MODES:
        raise ValueError(f"mode는 {QUALITATIVE_MODES}만 허용 (priority/threshold는 후속)")
    before = get_qualitative_mode()
    now = int(time.time())
    after = {
        "mode": mode,
        "updated": now,
        "approved_by": (approved_by or "")[:120],
        "note": (note or "")[:240],
    }
    db.kv_set(_QUAL_KEY, after)
    hist = db.kv_get(_QUAL_HIST_KEY) or []
    if not isinstance(hist, list):
        hist = []
    hist.insert(0, {
        "ts": now, "before": before.get("mode"), "after": mode,
        "approved_by": after["approved_by"], "note": after["note"],
        "gates": gates_snapshot or {},
    })
    db.kv_set(_QUAL_HIST_KEY, hist[:_QUAL_HIST_MAX])
    return get_qualitative_mode()


def qualitative_promotion_history(limit: int = 5) -> list[dict]:
    hist = db.kv_get(_QUAL_HIST_KEY) or []
    if not isinstance(hist, list):
        return []
    return hist[:limit]


def qualitative_promotion_status(metrics: dict | None = None) -> dict:
    """관리자 UI용 — 현재 모드 + (선택) 실측 게이트 스냅샷."""
    mode = get_qualitative_mode()
    out = {
        **mode,
        "allowed_modes": list(QUALITATIVE_MODES),
        "affects_combine": False,
        "affects_bot": False,
        "history": qualitative_promotion_history(5),
        "disclaimer": "정성 점수는 종합점수·매수 임계값·페이퍼 봇에 반영되지 않습니다.",
    }
    if metrics is not None:
        out["metrics"] = metrics
    return out


def effective_config(regime_result: dict | None, macro_result: dict | None,
                     base: SignalConfig | None = None,
                     flow_result: dict | None = None) -> tuple[SignalConfig, dict]:
    """국면·거시·시장수급을 반영한 '실효' 설정. base가 국면 적응(on)이면 약세·비우호 국면 / 외국인·
    기관 순매도에서 매수/강력매수 임계값을 상향한다. 반환: (config, {bump, reasons, effective_buy_threshold}).

    api._signals와 bot이 이 하나를 공유해 시그널 표시·자동매매가 동일 기준을 쓰게 한다.
    """
    base = base or get_config()
    info = {"bump": 0.0, "reasons": [], "effective_buy_threshold": base.buy_threshold}
    if base.regime_adaptive < 0.5:
        return base, info
    b = regime_mod.buy_threshold_bump(regime_result, macro_result, flow_result)
    if not b["bump"]:
        return base, info
    cfg = replace(base, buy_threshold=base.buy_threshold + b["bump"],
                  strong_buy_threshold=base.strong_buy_threshold + b["bump"])
    info = {"bump": b["bump"], "reasons": b["reasons"], "effective_buy_threshold": cfg.buy_threshold}
    return cfg, info
