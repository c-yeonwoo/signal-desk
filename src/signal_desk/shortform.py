"""숏폼 콘텐츠 생성 — 시그널+근거로 스크립트(자막/나레이션)와 세로 데이터 카드(SVG)를 만들어
검수 큐(db.shortform)에 draft로 적재한다. 발행은 반드시 사람 승인 후(별도 단계).

콘텐츠 관점(beconti)과 달리 우리는 '실사용 영상 분석'이 아니라 '시그널 데이터 → 카드'다.
스크립트 스키마 {time, caption, narration}은 beconti와 통일(추후 조립/공유 여지).

규제: 정보 제공·교육 목적. 투자권유·매수종용·수익보장 표현 금지. 스케일된 시세 기반이라
신뢰 불가한 구체 목표가·승률 수치는 노출하지 않고 정성 근거 중심으로.
"""

from __future__ import annotations

import logging
import uuid

from signal_desk import db, kb, llm, store
from signal_desk.reference import sectors
from signal_desk.signals import engine

log = logging.getLogger("signal_desk.shortform")

_KIND_KO = {"STRONG_BUY": "강력매수", "BUY": "매수"}
_DISCLAIMER = "정보 제공·교육 목적이며 투자 권유가 아닙니다. 투자 판단과 책임은 본인에게 있습니다."

_SCRIPT_SYS = (
    "너는 국내주식 숏폼(세로영상) 대본 작가다. 주어진 종목 시그널과 근거로 30~40초 분량의 한국어 "
    "숏폼 대본을 만든다. 규칙: (1) 정보 제공·교육 목적, 투자 권유·매수 종용·수익 보장 표현 절대 금지, "
    "(2) 구체적 목표주가·수익률·적중률 같은 수치는 쓰지 말고 근거를 정성적으로 설명, "
    "(3) 자연스럽고 담백한 구어체, 과장·감탄사 남발 금지, (4) 마지막 줄에 반드시 면책 문구를 넣는다."
)


def _reason_clean(reasons: list[str], n: int = 3) -> list[str]:
    """근거에서 앞머리 태그([기술] 등) 유지한 채 상위 n개, 각 60자 컷."""
    out = []
    for r in reasons or []:
        r = (r or "").strip()
        if not r or r.startswith("[추세]") and "차단" in r:  # 게이트 안내는 카드에 부적합
            continue
        out.append(r[:60])
        if len(out) >= n:
            break
    return out


def _pick_signals(limit: int) -> list:
    universe = store.load_universe()
    prices = store.load_price_series()
    if not universe or not prices:
        return []
    sigs = engine.evaluate(universe, prices, store.load_fundamentals(), sentiment=kb.sentiment_map())
    warned = store.load_warned_tickers()
    elig = [s for s in sigs if engine.is_buy(s.kind) and not s.event_risk and s.ticker not in warned]
    return sorted(elig, key=lambda s: s.score, reverse=True)[:limit]


def _script_for(name: str, ticker: str, kind: str, reasons: list[str]) -> dict:
    """LLM(Sonnet)로 {title, script:[{time,caption,narration}], caption, hashtags}. 실패 시 규칙 기반."""
    kind_ko = _KIND_KO.get(kind, "관심")
    clean = _reason_clean(reasons, 3)
    if llm.available():
        user = (f"종목: {name}({ticker})\n시그널: {kind_ko}\n근거:\n- " + "\n- ".join(clean) +
                '\n\nJSON으로만 응답: {"title": "훅 제목", '
                '"script": [{"time":"0-4s","caption":"화면자막","narration":"나레이션"}, ...(4~6줄)], '
                '"caption": "게시물 설명(2문장, 면책 포함)", "hashtags": ["#태그", ...5개]}')
        out = llm.complete_json(_SCRIPT_SYS, user, max_tokens=900, model=llm.NARRATIVE_MODEL)
        if out and isinstance(out.get("script"), list) and out["script"]:
            out.setdefault("title", f"오늘의 시그널 · {name}")
            out.setdefault("caption", _DISCLAIMER)
            out.setdefault("hashtags", ["#주식", "#시그널", f"#{name}", "#국내주식", "#투자정보"])
            # 면책 안전망: caption에 면책 없으면 덧붙임
            if "투자 권유" not in out["caption"] and "투자권유" not in out["caption"]:
                out["caption"] = f"{out['caption']} ※ {_DISCLAIMER}"
            return out
    # 규칙 기반 폴백
    lines = [{"time": "0-4s", "caption": f"오늘의 시그널 · {name}", "narration": f"오늘 주목할 종목, {name}입니다."},
             {"time": "4-8s", "caption": f"{kind_ko} 구간", "narration": f"현재 {kind_ko} 시그널이 나왔는데요."}]
    for i, r in enumerate(clean):
        body = r.split("] ", 1)[-1]
        lines.append({"time": f"{8 + i * 6}-{14 + i * 6}s", "caption": f"근거 {i + 1}", "narration": body})
    lines.append({"time": "end", "caption": "⚠️ 정보 제공용", "narration": _DISCLAIMER})
    return {"title": f"오늘의 시그널 · {name}", "script": lines, "caption": _DISCLAIMER,
            "hashtags": ["#주식", "#시그널", f"#{name}", "#국내주식", "#투자정보"]}


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _card_svg(name: str, ticker: str, kind: str, score: float, reasons: list[str], sector: str | None) -> str:
    """세로 1080x1920 데이터 카드(SVG, 자기완결). 브라우저 미리보기·후속 래스터화 공용."""
    kind_ko = _KIND_KO.get(kind, "관심")
    pill = "#0b7a3b" if kind == "STRONG_BUY" else "#22c55e"
    clean = _reason_clean(reasons, 3)
    rows = ""
    for i, r in enumerate(clean):
        y = 900 + i * 150
        rows += (f'<text x="120" y="{y}" fill="#e5e7eb" font-size="46" font-weight="600">'
                 f'{i + 1}. {_esc(r)}</text>')
    sec = _esc(sector or "")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1920" width="1080" height="1920">
  <rect width="1080" height="1920" fill="#0b1220"/>
  <rect x="0" y="0" width="1080" height="10" fill="{pill}"/>
  <text x="120" y="200" fill="#9ca3af" font-size="40">오늘의 시그널</text>
  <text x="120" y="320" fill="#ffffff" font-size="92" font-weight="800">{_esc(name)}</text>
  <text x="120" y="390" fill="#6b7280" font-size="40">{_esc(ticker)}{(" · " + sec) if sec else ""}</text>
  <rect x="120" y="470" rx="20" width="360" height="110" fill="{pill}"/>
  <text x="300" y="545" fill="#ffffff" font-size="58" font-weight="800" text-anchor="middle">{kind_ko}</text>
  <text x="540" y="545" fill="#e5e7eb" font-size="52" font-weight="700">종합점수 {score:+.2f}</text>
  <text x="120" y="760" fill="#9ca3af" font-size="44" font-weight="700">핵심 근거</text>
  {rows}
  <rect x="0" y="1740" width="1080" height="180" fill="#111827"/>
  <text x="120" y="1815" fill="#9ca3af" font-size="34">⚠️ {_esc(_DISCLAIMER)}</text>
</svg>'''


def generate(limit: int = 5, dry_run: bool = False, skip_recent_hours: int = 20) -> dict:
    """상위 매수 시그널로 숏폼 초안 생성 → 검수 큐 적재(draft). 최근 생성 종목은 중복 제외."""
    recent = db.shortform_recent_tickers(skip_recent_hours * 3600) if not dry_run else set()
    picks = [s for s in _pick_signals(max(limit * 3, 6)) if s.ticker not in recent][:limit]
    if not picks:
        return {"ok": False, "reason": "조건에 맞는 매수 시그널이 없거나 시세 데이터가 없습니다.",
                "created": [], "count": 0}
    made = []
    for s in picks:
        sector = sectors.sector_of(s.ticker)
        sc = _script_for(s.name, s.ticker, s.kind, s.reasons)
        svg = _card_svg(s.name, s.ticker, s.kind, s.score, s.reasons, sector)
        item = {"id": uuid.uuid4().hex, "ticker": s.ticker, "name": s.name, "kind": s.kind,
                "score": round(s.score, 2), "title": sc["title"], "script": sc["script"],
                "caption": sc["caption"], "hashtags": sc["hashtags"], "card_svg": svg, "status": "draft"}
        if not dry_run:
            db.shortform_add(item)
        made.append({k: item[k] for k in ("id", "ticker", "name", "kind", "score", "title")})
    log.info("숏폼 초안 생성: %d건", len(made))
    return {"ok": True, "created": made, "count": len(made)}
