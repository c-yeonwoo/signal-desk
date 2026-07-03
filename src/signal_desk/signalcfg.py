"""시그널 엔진 설정(팩터 가중치·임계값) 영속화 — 관리자가 조정하면 시그널·백테스트에 반영.

기본값은 engine.SignalConfig, 관리자 오버라이드는 db.kv('signal_config')에 저장한다.
api._signals / _backtest / bot이 모두 이 get_config()를 써서 동일 설정으로 계산한다.
"""

from __future__ import annotations

from signal_desk import db
from signal_desk.signals.engine import SignalConfig

_KEY = "signal_config"
# 관리자 조정 대상 필드(팩터 가중치 5종 + 매수/매도 임계값)
FIELDS = ["weight_technical", "weight_fundamental", "weight_valuation",
          "weight_reversion", "weight_qualitative",
          "strong_buy_threshold", "buy_threshold", "sell_threshold", "strong_sell_threshold"]


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
