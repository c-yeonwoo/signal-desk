"""감사 가설 생성기 — LLM에게 "이 숫자가 틀렸다면 왜일까"만 시킨다.

## 왜 에이전트에게 판정을 안 맡기나

2026-07-26 하네스 작업에서 세 번 틀렸고(안 산 기간 수수료 차감 / 동점을 시총순 정렬 /
대조군만 위상 평균), 세 번 다 잡아낸 건 리뷰가 아니라 **대조군**이었다. 같은 계열 모델끼리
검증시키면 같은 사각지대를 공유하고 서로 동의하는 쪽으로 수렴한다 — "방법론이 타당해 보입니다"는
버그가 세 개 있어도 나오는 문장이다.

그래서 역할을 나눈다. **판정은 기계(`tests/test_redteam.py`), 가설은 LLM.** 여기서 나오는
건 결론이 아니라 "확인해볼 것" 목록이고, 코드로 검증 가능한 것만 사람이 테스트로 승격한다.
이 모듈은 설정을 읽기만 하고 **아무것도 바꾸지 않는다** — advisor/climate shadow와 같은 규약이다.

## 저장 규칙

반증 방법(`falsifier`)이 없는 항목은 **버린다.** "데이터가 더 필요해 보입니다" 같은 문장은
영원히 참이라 아무것도 못 막는다. id는 내용 해시라 같은 지적이 매주 중복 적재되지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from signal_desk import db, llm

log = logging.getLogger(__name__)

MAX_HYPOTHESES = 5
_MAX_TOKENS = 4000

_SYSTEM = """너는 투자 시그널 서비스의 계측을 감사하는 사람이다. 개선안을 제안하지 마라.
네 임무는 **지금 보고 있는 숫자가 틀렸을 수 있는 이유**를 찾아내는 것 하나다.

규칙:
- 각 가설은 반드시 반증 가능해야 한다. "무엇을 관측하면 이 가설이 거짓으로 판명되는가"를
  구체적으로 써라. 반증 방법을 쓸 수 없으면 그 가설은 내지 마라.
- 파라미터를 바꾸라거나 가중치를 조정하라는 말은 하지 마라. 너에게는 판정권이 없다.
- "표본이 더 필요하다", "지속적인 모니터링이 필요하다" 같은 항상 참인 문장은 금지.
- 이미 코드가 막고 있는 것을 다시 지적하지 마라(아래 '이미 검사 중' 참고).
- 계측 오류·정의 불일치·분모 오염·생존편향·이중계산·시점 불일치처럼 **숫자를 거짓으로 만드는
  기전**에 집중하라.

과거에 실제로 발견된 오류의 예(이 수준의 구체성을 요구한다):
- 매수 0건인 기간에도 거래비용을 차감해, 아무것도 안 산 것이 손실로 기록됐다.
- 점수 동점을 유니버스 순서로 정렬해서 "점수가 없으면 대형주를 산다"가 몰래 섞였다.
- 모멘텀 팩터가 252거래일 이력을 요구하는데 캐시가 268일이라, 커버리지 5.9%로 조용히 빠진
  채 "8팩터 백테스트"라는 이름이 붙어 있었다.

JSON만 출력한다. 각 필드는 짧게(claim·falsifier는 각 200자 이내, title은 40자 이내):
{"hypotheses": [{"target": "어느 지표·모듈", "title": "한 줄 요약",
 "claim": "무엇이 어떻게 틀렸을 수 있는지", "falsifier": "무엇을 관측하면 거짓인지",
 "check_hint": "확인 방법(가능하면 테스트 이름이나 명령)",
 "severity": "high|medium|low"}]}"""

_ALREADY_CHECKED = """이미 기계적으로 검사 중(다시 지적 금지):
- 룩어헤드: 미래 가격을 바꿔도 과거 시점 출력이 불변인지 전 replay 경로에서 검사
- 누수: 점수와 수익률의 짝을 셔플하면 판별력이 사라지는지 검사
- 없는 엣지 생성: 랜덤워크 시장에서 판별력이 나오면 실패
- 도구 고장: 엣지가 실재하는 합성 시장에서 못 잡으면 실패
- base rate: 정밀도류 비율에 기준선·리프트·신뢰구간이 없으면 실패
- 표본·커버리지: 리밸런스 30회 미만, 팩터 커버리지 60% 미만이면 판정 자체를 차단
- 점수 팩터 ⊆ PIT/factor_ic: combine에 들어가는 팩터(short 포함)가 스냅샷·IC에 있는지 검사
- 성숙 0의 사유: matured_primary=0이면 stale/미매칭/정상 미성숙을 blocked_reason으로 구분"""


def _hid(item: dict) -> str:
    raw = f"{item.get('target')}|{item.get('title')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def collect_context() -> dict:
    """감사 대상 스냅샷. 실패하는 조각은 통째로 빼고 진행한다(감사가 서비스를 멈추면 안 된다)."""
    ctx: dict = {"generated": time.strftime("%Y-%m-%d %H:%M")}

    def _try(key, fn):
        try:
            ctx[key] = fn()
        except Exception as exc:                      # noqa: BLE001 — 스냅샷 수집은 best-effort
            log.warning("감사 컨텍스트 수집 실패(%s): %s", key, exc)
            ctx[key] = {"error": str(exc)[:200]}

    from signal_desk import signalcfg, store
    from signal_desk.signals import accuracy

    _try("engine_config", signalcfg.get_dict)
    _try("data_health", lambda: {"drift": store.signal_drift()})
    _try("accuracy", lambda: _accuracy_snapshot(accuracy))
    _try("advisor_shadow", _shadow_snapshot)
    return ctx


def _accuracy_snapshot(accuracy) -> dict:
    from signal_desk import store
    df = store.load_signal_history()
    if df.empty:
        return {"ready": False}
    out = accuracy.realized_accuracy(df.to_dict("records"), store.load_all_dated_closes())
    keep = ("buy_precision_pct", "buy_sample", "buy_lift_pp", "buy_precision_ci_pp",
            "sell_precision_pct", "sell_sample", "sell_lift_pp", "sell_precision_ci_pp",
            "baseline", "lift_min_pp", "factor_ic", "coverage")
    return {k: out.get(k) for k in keep if k in out}


def _shadow_snapshot() -> dict:
    from signal_desk import store
    from signal_desk.signals import advisor_shadow
    out = advisor_shadow.summary(store.load_all_dated_closes())
    keep = ("ready", "runs", "matured_pairs", "delta_pct", "delta_ci95_pp",
            "delta_significant", "verdict_ready", "min_samples",
            "paired_n", "paired_delta_pct", "paired_delta_ci95_pp",
            "paired_delta_significant", "paired_verdict_ready")
    return {k: out.get(k) for k in keep if k in out}


def generate(context: dict | None = None) -> dict:
    """가설 생성 → 저장. LLM이 없으면 조용히 비활성(기존 shadow들과 같은 규약)."""
    if not llm.available():
        return {"ready": False, "reason": "ANTHROPIC_API_KEY 미설정 — 가설 생성 비활성",
                "saved": 0}
    ctx = context if context is not None else collect_context()
    user = (f"{_ALREADY_CHECKED}\n\n지금 계측 스냅샷:\n"
            f"{json.dumps(ctx, ensure_ascii=False, default=str)[:12000]}\n\n"
            f"가설을 최대 {MAX_HYPOTHESES}개. 반증 방법을 못 쓰겠으면 그 항목은 빼라.")
    raw = llm.complete(_SYSTEM, user, max_tokens=_MAX_TOKENS)
    if not raw:
        return {"ready": False, "reason": "LLM 응답 없음", "saved": 0}
    items = parse_hypotheses(raw)
    if not items:
        return {"ready": False, "reason": "가설을 해석하지 못했습니다", "saved": 0}
    return {"ready": True, "saved": save(items), "dropped": _dropped(items)}


def parse_hypotheses(raw: str) -> list[dict]:
    """관대한 파서 — 잘린 응답에서도 **완결된 항목만** 건져낸다.

    처음엔 `llm.complete_json`을 썼는데 응답이 max_tokens에서 잘리면 `json.loads`가 실패해
    멀쩡한 앞쪽 가설까지 통째로 버려졌다("LLM 응답 없음"으로만 보여 원인도 안 보였다).
    잘림은 예외 상황이 아니라 기본값에 가깝다.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    try:
        out = json.loads(text)
        if isinstance(out, dict) and isinstance(out.get("hypotheses"), list):
            return [h for h in out["hypotheses"] if isinstance(h, dict)]
    except (json.JSONDecodeError, TypeError):
        pass
    return _salvage_objects(text)


def _salvage_objects(text: str) -> list[dict]:
    """중괄호 균형을 세며 완결된 객체만 뽑는다(문자열 안의 괄호·이스케이프는 건너뛴다).

    바깥 `{"hypotheses": [...]}`는 잘려서 영원히 안 닫히므로 **중첩된 안쪽 객체까지** 봐야 한다.
    깊이 0으로 돌아올 때만 수집하면 아무것도 못 건진다.
    """
    items, stack, in_str, esc = [], [], False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            try:
                obj = json.loads(text[start:i + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "title" in obj:
                items.append(obj)
    return items


def save(items: list[dict]) -> int:
    """반증 방법이 있는 것만 저장. 반환값은 저장된 개수."""
    saved = 0
    for item in items[:MAX_HYPOTHESES]:
        if not isinstance(item, dict):
            continue
        if not (item.get("falsifier") or "").strip():
            continue                    # 반증 불가능한 지적은 아무것도 막지 못한다
        if not (item.get("title") or "").strip():
            continue
        db.audit_hypothesis_upsert({**item, "id": _hid(item)})
        saved += 1
    return saved


def _dropped(items: list[dict]) -> int:
    return sum(1 for i in items[:MAX_HYPOTHESES]
               if isinstance(i, dict) and not (i.get("falsifier") or "").strip())


def summary(limit: int = 30) -> dict:
    """관리자 화면용. 이 큐는 관측이며 엔진·봇에 아무 영향을 주지 않는다."""
    items = db.audit_hypothesis_list(limit=limit)
    return {
        "available": llm.available(),
        "pending": db.audit_pending_count(),
        "items": items,
        "disclaimer": ("가설 목록이다 — 판정이 아니다. 엔진·봇에 아무 영향을 주지 않는다. "
                       "확인된 것만 사람이 tests/test_redteam.py로 승격한다."),
    }
