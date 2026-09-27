"""실제 적용된 판단 정책의 내용 주소. 수익확률이나 성과 승격 상태는 나타내지 않는다."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path

SIGNAL_POLICY_VERSION = "signal-policy-v1"
EXECUTION_POLICY_VERSION = "paper-execution-v2-advisor-cache"
SCORE_SEMANTICS = "uncalibrated_score_strength"


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()[:24]


@lru_cache(maxsize=1)
def _signal_source_id() -> str:
    base = Path(__file__).resolve().parent
    files = (Path(__file__), base / "engine.py", base / "decision.py",
             base / "execution_gate.py", base.parent / "signalcfg.py")
    return hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()[:24]


@lru_cache(maxsize=1)
def _execution_source_id() -> str:
    base = Path(__file__).resolve().parent.parent
    files = (Path(__file__), base / "strategy.py", base / "bot.py",
             base / "signals" / "risk.py", base / "broker" / "paper.py",
             base / "broker" / "execution.py", base / "llm.py", base / "signals" / "advisor.py",
             base / "signals" / "advisor_cache.py", base / "signals" / "advisor_shadow.py")
    return hashlib.sha256(b"".join(p.read_bytes() for p in files)).hexdigest()[:24]


def signal_policy_id(market: str, effective_config) -> str:
    """같은 엔진 코드·유효 설정·시장에 같은 ID. 입력 데이터 자체의 ID는 별도로 저장한다."""
    config = asdict(effective_config) if is_dataclass(effective_config) else dict(effective_config)
    return _digest({"version": SIGNAL_POLICY_VERSION, "source": _signal_source_id(),
                    "market": market, "config": config})


def execution_policy_id(market: str, style: str, *, exposure: float,
                        signal_id: str, risk_limits: dict) -> str:
    """같은 신호 정책이라도 성향·당시 노출 한도가 다르면 별도 실행 정책이다."""
    return _digest({"version": EXECUTION_POLICY_VERSION, "source": _execution_source_id(),
                    "market": market, "style": style, "exposure": exposure,
                    "signal_policy_id": signal_id, "risk_limits": risk_limits})
