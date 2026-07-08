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

from signal_desk import db, kb, llm, signalcfg, store
from signal_desk.reference import sectors
from signal_desk.signals import engine, macro, regime

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


_RECENT_SEC = 20 * 3600  # 이 기간 내 이미 숏폼 만든 종목은 '중복'으로 표시(자동 모드에선 스킵)
_SCORE_CANDIDATE = 1.5      # 매수 시그널이 아니어도 종합점수 이 이상이면 후보(숏폼 소재거리)
_SENTIMENT_CANDIDATE = 0.5  # 정성 호재(KB 감성 점수)가 이 이상이면 후보(매수·점수와 무관하게)


def _candidacy_basis(s) -> str | None:
    """이 시그널이 숏폼 '소재거리'가 되는 이유(라벨). 아니면 None. 악재·경고는 호출측에서 이미 제외.
    매수가 아니어도 (1) 종합점수 1.5+ (2) 정성 호재 0.5+ 면 '관심' 소재로 후보에 올린다."""
    if engine.is_buy(s.kind):
        return "매수 시그널"
    if s.score >= _SCORE_CANDIDATE:
        return f"고점수 {s.score:+.1f}"
    if s.has_qualitative and (s.qualitative_score or 0) >= _SENTIMENT_CANDIDATE:
        return "정성 호재"
    return None


def _eligible_signals() -> list:
    """숏폼 소재거리 시그널(악재·경고 제외)을 점수 내림차순으로. 시그널 탭(_signals)과 동일 계산.
    매수뿐 아니라 고점수(1.5+)·정성 호재(0.5+)도 포함 — '만들 만한 소재'가 있으면 후보로."""
    universe = store.load_universe()
    prices = store.load_price_series()
    if not universe or not prices:
        return []
    # 관리자 튜닝 + 국면 적응형 매수기준(effective_config) — 이걸 빼면 후보가 시그널 탭과 어긋난다.
    reg = regime.classify(prices)
    mread = macro.read(store.load_macro(), extra=store.load_macro_kr())
    cfg, _ = signalcfg.effective_config(reg, mread, flow_result=store.load_market_flow())
    sigs = engine.evaluate(universe, prices, store.load_fundamentals(), config=cfg,
                           sentiment=kb.sentiment_map(), flows=store.load_flows())
    warned = store.load_warned_tickers()
    # 악재(event_risk)·투자경고는 숏폼에 부적합 → 항상 제외. 그 외엔 소재거리가 있으면 포함.
    elig = [s for s in sigs if not s.event_risk and s.ticker not in warned and _candidacy_basis(s)]
    return sorted(elig, key=lambda s: s.score, reverse=True)


def candidates(limit: int = 20) -> list[dict]:
    """생성 전 '후보 목록' — 소재거리(매수·고점수·정성 호재)를 점수순으로 근거와 함께 보여준다
    (생성·저장 없음). basis=후보가 된 이유. 관리자가 골라 generate(tickers=[...])로 만든다."""
    recent = db.shortform_recent_tickers(_RECENT_SEC)
    return [{"ticker": s.ticker, "name": s.name, "kind": s.kind, "score": round(s.score, 2),
             "basis": _candidacy_basis(s), "reasons": _reason_clean(s.reasons, 3),
             "recent": s.ticker in recent}
            for s in _eligible_signals()[:limit]]


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
    # 매수=초록(강력=진초록), 매수 아님(관심 소재)=파랑 — 매수 아닌데 초록이면 매수로 오독될 수 있어 구분.
    pill = "#0b7a3b" if kind == "STRONG_BUY" else "#22c55e" if kind == "BUY" else "#0ea5e9"
    clean = _reason_clean(reasons, 3)
    rows = ""
    for i, r in enumerate(clean):
        y = 900 + i * 150
        rows += (f'<text x="120" y="{y}" fill="#e5e7eb" font-size="46" font-weight="600">'
                 f'{i + 1}. {_esc(r)}</text>')
    sec = _esc(sector or "")
    # width/height는 고정하지 않는다 — viewBox만 두면 컨테이너 크기에 맞춰 스케일(미리보기 220px 등)된다.
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1920" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block">
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


def generate(tickers: list[str] | None = None, limit: int = 5, dry_run: bool = False) -> dict:
    """숏폼 초안 생성 → 검수 큐 적재(draft). tickers 지정 시 그 종목만(선택 생성, 시그널 순 유지),
    없으면 상위 매수 시그널 top N(자동, 최근 생성 중복 제외)."""
    elig = _eligible_signals()
    if tickers:
        want = set(tickers)
        picks = [s for s in elig if s.ticker in want]  # 선택분만(순서=시그널 순)
    else:
        recent = db.shortform_recent_tickers(_RECENT_SEC) if not dry_run else set()
        picks = [s for s in elig if s.ticker not in recent][:limit]
    if not picks:
        return {"ok": False, "reason": "소재거리(매수·고점수·정성 호재)가 없거나 시세 데이터가 없습니다.",
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


# ---------- 성과(track record) 숏폼 — 봇의 실제 성과를 콘텐츠로(봇↔숏폼 시너지) ----------
_PERF_DISCLAIMER = ("모의투자(페이퍼) 성과이며 과거 성과가 미래 수익을 보장하지 않습니다. "
                    "정보 제공용이며 투자 권유가 아닙니다.")


def _perf_card_svg(label: str, ret_pct, days: int, mdd, n_trades: int, unit: str) -> str:
    """세로 1080x1920 성과 카드 — 성향·수익률·기간·최대낙폭·거래수."""
    color = "#22c55e" if (ret_pct or 0) >= 0 else "#ef4444"
    ret_s = f"{ret_pct:+.1f}%" if ret_pct is not None else "–"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 1920" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;display:block">
  <rect width="1080" height="1920" fill="#0b1220"/>
  <rect x="0" y="0" width="1080" height="10" fill="{color}"/>
  <text x="120" y="220" fill="#9ca3af" font-size="44">공용 자동매매 봇 성과</text>
  <text x="120" y="340" fill="#ffffff" font-size="86" font-weight="800">{_esc(label)}</text>
  <text x="120" y="640" fill="#9ca3af" font-size="52">최근 {days}일 수익률</text>
  <text x="120" y="800" fill="{color}" font-size="200" font-weight="800">{ret_s}</text>
  <text x="120" y="1000" fill="#e5e7eb" font-size="48">최대낙폭 {_esc(f"{mdd:.1f}%" if mdd is not None else "–")} · 거래 {n_trades}건</text>
  <rect x="0" y="1720" width="1080" height="200" fill="#111827"/>
  <text x="120" y="1795" fill="#9ca3af" font-size="30">⚠️ {_esc(_PERF_DISCLAIMER)}</text>
</svg>'''


def generate_performance(style: str = "balanced", market: str = "kr", dry_run: bool = False) -> dict:
    """레퍼런스 봇의 track record를 숏폼 초안으로 → 검수 큐 적재. '이 시그널이 실제로 이렇게 됐다'를
    콘텐츠화(봇↔숏폼 시너지). 모의투자·면책 명시."""
    from signal_desk import bot, strategy
    uid = {v: k for k, v in bot.REFERENCE_BOTS.items()}.get(style)
    if not uid:
        return {"ok": False, "reason": f"알 수 없는 성향: {style}", "count": 0, "created": []}
    p = bot.performance(uid, market)
    label = strategy.STYLE_LABEL.get(style, style)
    unit = "$" if market == "us" else "원"
    ret, days, mdd, n = p.get("return_pct"), p.get("days", 0), p.get("max_drawdown_pct"), p.get("n_trades", 0)
    if not days:
        return {"ok": False, "reason": "아직 성과 데이터가 없습니다(봇 운용 후 생성)", "count": 0, "created": []}
    title = f"{label} 자동매매 봇 · 최근 {days}일 {ret:+.1f}%" if ret is not None else f"{label} 봇 성과"
    script = [
        {"time": "0-4s", "caption": f"{label} 자동매매 봇", "narration": f"공용 {label} 봇의 최근 성과입니다."},
        {"time": "4-9s", "caption": f"{days}일 수익률 {ret:+.1f}%" if ret is not None else "성과 집계",
         "narration": f"최근 {days}일 수익률은 {ret:+.1f}%," if ret is not None else "집계 기간입니다."},
        {"time": "9-14s", "caption": f"최대낙폭 {mdd:.1f}%", "narration": f"최대낙폭 {mdd:.1f}%, 거래 {n}건이었습니다."},
        {"time": "end", "caption": "⚠️ 모의투자 성과", "narration": _PERF_DISCLAIMER},
    ]
    item = {"id": uuid.uuid4().hex, "ticker": "_PERF", "name": f"{label} 봇", "kind": "PERF",
            "score": ret, "title": title, "script": script, "caption": _PERF_DISCLAIMER,
            "hashtags": ["#자동매매", "#퀀트", "#주식", f"#{label}", "#수익률공개"],
            "card_svg": _perf_card_svg(label, ret, days, mdd, n, unit), "status": "draft"}
    if not dry_run:
        db.shortform_add(item)
    return {"ok": True, "count": 1, "created": [{k: item[k] for k in ("id", "ticker", "name", "kind", "score", "title")}]}
