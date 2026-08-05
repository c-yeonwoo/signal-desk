"""사전등록 — 무엇을 언제 어떤 설정으로 재기로 했는지를 **미리** 못 박는다.

## 왜 파일이고, 왜 git인가

`harness_last.json` 1슬롯을 덮어쓰는 구조에서는 "판정"이 곧 **마지막으로 돌린 결과**였다.
8조합 스윕을 돌리면 판별력이 전혀 없어도 하나가 95%를 넘을 확률이 33.7%인데, `cli.py`가
combos 루프 안에서 저장해 그 한 칸이 보드에 남았다. 초록 칸을 골라 쓰는 것은 측정이 아니라
고르기다.

그래서 정본은 **사전등록된 조합만**이고, 등록은 `docs/preregistered.toml`에 적어 **커밋**한다.
DB에 두면 내가 나중에 조용히 고칠 수 있어서 '사전'이라는 말이 성립하지 않는다 — 파일이면
변경이 커밋 이력에 남는다. 읽기는 `tomllib`(3.11+ 표준 라이브러리)로 한다: 새 의존성 0.

## 다중검정

같은 가설을 interim·final 두 번 보기로 했다(요건 도달이 2027-02라 그때까지 눈을 감을 수 없다).
두 번 보면 두 번의 기회이므로 문턱을 올린다 — Šidák로 n=2면 97.47%다.

`n`은 **파일에 등록된 pit look 총수**이고 lock 상태와 무관하다. `status != locked`인 수로 세면
interim이 확정되는 순간 n이 2→1로 줄어 final의 문턱이 저절로 95%로 내려간다. 사후 완화다.

엄밀히는 interim·final은 독립 다중검정이 아니라 같은 가설의 **순차 관측**(nested data)이므로
정확한 처리는 alpha-spending 경계다. 두 관측은 양의 상관이라 Šidák은 **필요한 것보다 엄격한
쪽으로** 틀린다 — 1인 랩에서 그 방향의 오차는 받아들인다.

관련: docs/prd-harness-preregistration.md (F1·F9·F11·F12)
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

DEFAULT_PATH = Path("docs/preregistered.toml")

# 문턱의 기준 유의수준. Šidák: α₁ = 1 − (1 − α)^(1/n)
ALPHA = 0.05

# 정본으로 삼을 수 있는 점수 출처.
#  - "pit"    : 그날 라이브가 낸 점수 스냅샷. 8팩터 전부, 룩어헤드 원리적으로 없음.
#  - "price6" : 시점별 재무(FY+1-04-01 규칙)로 복원한 6팩터. 수급·공매도(가중 0.35)는
#               시계열 이력이 없어 백필 불가라 빠진다.
# "price"(3팩터)는 제외한다 — `_score_series`가 technical·reversion·momentum 셋만 계산하는데
# 그 결과를 "8팩터 시그널의 판별력"으로 읽으면 이름과 다른 전략을 측정한 것이 된다.
# price6가 허용되는 이유는 팩터가 더 많아서가 아니라 **이름이 정직해서**다 — 등록할 때
# hypothesis에 "6팩터이고 수급·공매도는 빠졌다"를 반드시 적는다.
CANONICAL_SOURCES = ("pit", "price6")

_HARNESS_KEYS = ("hold", "cost_pct", "trials", "exposure")


def sidak_threshold_pct(n: int) -> float:
    """등록 look이 n개일 때 요구하는 백분위 문턱(%). n=1 → 95.00, n=2 → 97.47, n=8 → 99.36."""
    n = max(1, int(n))
    alpha1 = 1 - (1 - ALPHA) ** (1 / n)
    return round((1 - alpha1) * 100, 2)


def canonical_looks(looks: list[dict]) -> list[dict]:
    return [lk for lk in looks if (lk.get("score_source") or "") in CANONICAL_SOURCES]


def threshold_for(looks: list[dict], look_id: str) -> float:
    """그 look에 적용할 문턱. **lock 상태를 보지 않는다**(F9-a).

    `looks`에 `status`가 섞여 있어도 무시한다 — 문턱은 파일이 정하고, 파일이 바뀌지 않으면
    문턱도 바뀌지 않는다.
    """
    n = len(canonical_looks(looks)) or 1
    return sidak_threshold_pct(n)


def config_hash(cfg: dict) -> str:
    """설정 dict → 안정 해시 12자. 키 순서·부동소수 표기에 흔들리지 않게 정규화한다."""
    norm = {k: (round(v, 6) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
            for k, v in sorted((cfg or {}).items())}
    blob = json.dumps(norm, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _merged(base: dict, look: dict, key: str) -> dict:
    """`[base.X]`를 상속하고 look의 `[looks.X]`로 덮는다(TOML에 앵커가 없어서)."""
    out = dict(base.get(key) or {})
    out.update(look.get(key) or {})
    return out


def load(path: Path | str | None = None) -> dict:
    """사전등록 파일을 읽어 검증한다.

    반환: `{ok, reason, base, looks, n_canonical, threshold_pct, path}`.
    실패해도 예외를 던지지 않는다 — 보드가 **이유를 화면에 낼 수 있어야** 하기 때문이다.
    조용히 빈 목록을 돌려주면 "등록이 없다"와 "파일이 깨졌다"가 같아 보인다.
    """
    p = Path(path) if path else DEFAULT_PATH
    out: dict = {"ok": False, "reason": "", "base": {}, "looks": [],
                 "n_canonical": 0, "threshold_pct": sidak_threshold_pct(1), "path": str(p)}
    if not p.exists():
        out["reason"] = f"사전등록 파일 없음 ({p}) — 정본 판정을 낼 수 없다"
        return out
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                       # noqa: BLE001 — 이유를 화면에 내야 한다
        out["reason"] = f"사전등록 파싱 실패: {type(e).__name__}: {e}"
        return out

    base = raw.get("base") or {}
    looks_raw = raw.get("looks") or []
    if not looks_raw:
        out["reason"] = "등록된 look이 없다([[looks]] 비어 있음)"
        return out

    base_cfg_hash = config_hash(base.get("config") or {})
    base_hz = {k: (base.get("harness") or {}).get(k) for k in _HARNESS_KEYS}

    looks: list[dict] = []
    seen: set[str] = set()
    for lk in looks_raw:
        lid = str(lk.get("id") or "").strip()
        if not lid:
            out["reason"] = "id 없는 look이 있다"
            return out
        if lid in seen:
            out["reason"] = f"id 중복: {lid}"
            return out
        seen.add(lid)

        src = str(lk.get("score_source") or base.get("score_source") or "")
        cfg = _merged(base, lk, "config")
        hz_cfg = _merged(base, lk, "harness")
        req = _merged(base, lk, "requirement")
        dec = _merged(base, lk, "decision")

        if src not in CANONICAL_SOURCES:
            out["reason"] = (f"{lid}: score_source={src!r}는 정본이 될 수 없다 "
                             f"(허용: {', '.join(CANONICAL_SOURCES)})")
            return out
        # 같은 가설의 순차 관측인데 설정이 다르면 순차가 아니라 별개 실험이다 → Šidák 가정도 깨진다.
        if config_hash(cfg) != base_cfg_hash:
            out["reason"] = (f"{lid}: look의 설정이 [base.config]와 다르다 — 같은 가설의 순차 관측이 "
                             f"아니라 별개 실험이다. 별개로 보려면 family를 나눠 등록할 것")
            return out
        if {k: hz_cfg.get(k) for k in _HARNESS_KEYS} != base_hz:
            out["reason"] = (f"{lid}: look의 하네스 설정이 [base.harness]와 다르다 "
                             f"— 같은 가설의 순차 관측이 아니다")
            return out
        for k in ("min_effective_periods", "min_pit_dates"):
            if not isinstance(req.get(k), int) or req[k] <= 0:
                out["reason"] = f"{lid}: requirement.{k}가 양의 정수여야 한다"
                return out

        looks.append({
            "id": lid,
            "role": str(lk.get("role") or "final"),
            "family": str(base.get("family") or ""),
            "score_source": src,
            "market": str(lk.get("market") or base.get("market") or "kr"),
            "hypothesis": (lk.get("hypothesis") or "").strip(),
            "registered_at": str(lk.get("registered_at") or ""),
            "config": cfg,
            "config_hash": config_hash(cfg),
            "harness": hz_cfg,
            "requirement": req,
            "decision": dec,
        })

    out["ok"] = True
    out["base"] = base
    out["looks"] = looks
    out["n_canonical"] = len(canonical_looks(looks))
    out["threshold_pct"] = sidak_threshold_pct(out["n_canonical"])
    return out


def config_agrees_with_engine(cfg: dict) -> tuple[bool, str]:
    """사전등록 설정이 **지금 돌아가는 엔진**과 같은지. (ok, 사유).

    설정의 진실이 세 곳에 있다 — `engine.SignalConfig` 소스 기본값, `kv:signal_config` 오버라이드,
    이 파일. H1(technical 0.35→0)처럼 **소스 상수**를 바꾸면 사전등록이 조용히 낡는다.
    낡은 등록으로 확정한 판정은 잰 것과 돌아가는 것이 다르므로 증거가 아니다.
    """
    from signal_desk import signalcfg

    live = signalcfg.get_config()
    diffs = []
    for k, v in (cfg or {}).items():
        if not hasattr(live, k):
            diffs.append(f"{k}: 엔진에 없는 필드")
            continue
        cur = getattr(live, k)
        if isinstance(v, (int, float)) and isinstance(cur, (int, float)) \
                and not isinstance(v, bool) and not isinstance(cur, bool):
            if abs(float(cur) - float(v)) > 1e-9:
                diffs.append(f"{k}: 등록 {v} vs 엔진 {cur}")
        elif str(cur) != str(v):
            diffs.append(f"{k}: 등록 {v!r} vs 엔진 {cur!r}")
    if diffs:
        return False, "사전등록 설정이 현재 엔진과 다르다 — " + " · ".join(diffs[:6])
    return True, ""


def board_status(locked: dict | None, *, current_hash: str) -> str:
    """보드에 뜰 상태. `pending` | `locked` | `invalidated`.

    확정된 판정도 그 뒤 설정이 바뀌면 무효다 — 잰 것과 돌아가는 것이 같아야 판정이 살아 있다.
    """
    if not locked:
        return "pending"
    return "locked" if str(locked.get("config_hash") or "") == str(current_hash) else "invalidated"


def progress(look: dict, *, effective_periods: int, pit_dates: int) -> dict:
    """요건 진척. 둘 다 충족해야(AND) 확정 대상이다.

    `remaining_pit_dates`는 거래일 수다 — 달력일로 환산하지 않는다. 휴장일 달력을 여기서 추측하면
    "언제까지"가 조용히 틀린 숫자가 된다(호출자가 실제 거래일로 센다).
    """
    req = look.get("requirement") or {}
    need_eff = int(req.get("min_effective_periods") or 0)
    need_pit = int(req.get("min_pit_dates") or 0)
    eff, pit = int(effective_periods or 0), int(pit_dates or 0)
    return {
        "min_effective_periods": need_eff, "effective_periods": eff,
        "min_pit_dates": need_pit, "pit_dates": pit,
        "remaining_effective_periods": max(0, need_eff - eff),
        "remaining_pit_dates": max(0, need_pit - pit),
        "met": eff >= need_eff and pit >= need_pit,
    }


# ------------------------------------------------------- 파라미터 변경 게이트 (N2)

def verdict_state(board: dict | None) -> tuple[bool, str]:
    """정본(final) 판정이 파라미터 변경을 허용하는 상태인가. (proven, 사유).

    `proven`은 **role=final 이 locked 이고 판정이 '판별력 있음'** 일 때만 True다.
    interim 통과는 채택 근거가 아니다(등록 파일의 `if_pass`에 그렇게 적혀 있다).
    """
    if not board or not board.get("ready"):
        return False, ((board or {}).get("reason") or "판별력 보드 없음")
    final = next((lk for lk in (board.get("looks") or []) if lk.get("role") == "final"), None)
    if final is None:
        return False, "정본(final) look이 등록돼 있지 않다"
    if final.get("status") != "locked":
        return False, f"정본 판정 미확정 — {final.get('verdict')}: {final.get('verdict_why') or ''}".strip()
    if final.get("verdict") != "판별력 있음":
        return False, f"정본 판정이 '{final.get('verdict')}' — 변경 근거가 없다"
    return True, ""


def change_allowed(board: dict | None, *, automated: bool,
                   override_reason: str = "") -> tuple[bool, str, bool]:
    """가중치·문턱을 바꿔도 되는가. (allowed, 사유, unproven).

    두 경로를 **다르게** 다룬다.

    - **자동 제안(`automated=True`)은 하드 차단.** 판정 없이 LLM이 가중치를 제안하고 사람은
      승인 버튼만 누르는 경로가 곧 곡선 맞추기다. 게이트가 열릴 때까지 큐를 비워 둔다 —
      쌓인 큐는 화면 배지로 떠서 승인을 유도하는 압력이 된다.
    - **수동은 사유를 받고 통과시킨다.** 순수하게 잠그면 진짜 바꿔야 할 때 `engine.py` 소스를
      직접 편집하는 우회로가 생기고(H1이 그랬다) 그 변경은 이력에 남지 않는다. 통과시키되
      `unproven=True`로 표시해 이력과 화면에 남긴다 — 미검증 변경을 **재무제표에 기록**한다.

    `unproven`이 True면 호출자는 그 사실을 `signalcfg.append_history`에 함께 남겨야 한다.
    """
    proven, why = verdict_state(board)
    if proven:
        return True, "", False
    if automated:
        return False, (f"판정 전에는 자동 제안을 만들지 않습니다 — {why}. "
                       f"측정되지 않은 것을 근거로 파라미터를 바꾸지 않습니다."), False
    if not (override_reason or "").strip():
        return False, (f"미검증 상태에서 값을 바꾸려면 사유가 필요합니다 — {why}. "
                       f"`override_reason` 에 왜 지금 바꾸는지 적어 주세요(이력에 남습니다)."), True
    return True, "", True
