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


_INTRO_KICKER = "오늘의 시그널"
_CONNECT = ["먼저", "다음으로", "여기에", "마지막으로"]  # 근거 나레이션 연결어(순차 공개 흐름)


def _wrap(text: str, n: int = 13) -> list[str]:
    """긴 근거 문장을 SVG tspan 여러 줄로 줄바꿈(공백 우선, 긴 토큰은 강제 분할, 최대 4줄)."""
    text = str(text or "").strip()
    if not text:
        return []
    out, line = [], ""
    for w in text.split(" "):
        while len(w) > n:  # 한 토큰이 너무 길면(붙여쓴 한글 등) 글자수로 강제 분할
            if line:
                out.append(line); line = ""
            out.append(w[:n]); w = w[n:]
        if len(line) + len(w) + (1 if line else 0) <= n:
            line = f"{line} {w}".strip()
        else:
            if line:
                out.append(line)
            line = w
    if line:
        out.append(line)
    return out[:4]


def _pill_color(kind: str) -> str:
    # 매수=초록(강력=진초록), 매수 아님(관심 소재)=파랑 — 매수 아닌데 초록이면 매수로 오독될 수 있어 구분.
    return "#0b7a3b" if kind == "STRONG_BUY" else "#22c55e" if kind == "BUY" else "#0ea5e9"


def _load_bg() -> str | None:
    """카드 배경 이미지 URL(관리자 설정, db kv 'shortform_bg'). 없으면 None(단색 배경).
    data URI는 장면마다 SVG에 박혀 DB가 커지므로 http(s) URL만 쓴다(에셋은 사용자가 호스팅·라이선스)."""
    try:
        url = db.kv_get("shortform_bg")
    except Exception:
        url = None
    return url or None


def _svg_open(bar: str, bg: str | None = None) -> str:
    """세로 1080x1920 프레임 공통 헤더 — 배경(선택 사진+scrim) + 상단 컬러바. width/height 미고정(스케일)."""
    head = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'viewBox="0 0 1080 1920" preserveAspectRatio="xMidYMid meet" '
            'style="width:100%;height:auto;display:block">')
    if bg:  # 배경 사진(호스팅 URL) 위에 어두운 scrim — 데이터 텍스트 가독성 유지
        base = (f'<image href="{_esc(bg)}" xlink:href="{_esc(bg)}" x="0" y="0" width="1080" height="1920" '
                f'preserveAspectRatio="xMidYMid slice"/>'
                f'<rect width="1080" height="1920" fill="#0b1220" opacity="0.66"/>')
    else:
        base = '<rect width="1080" height="1920" fill="#0b1220"/>'
    return head + base + f'<rect x="0" y="0" width="1080" height="12" fill="{bar}"/>'


def _intro_svg(name: str, ticker: str, kind: str, score: float, sector: str | None,
               bg: str | None = None) -> str:
    """인트로/썸네일 템플릿(재사용) — '오늘의 시그널' + 종목명 + ticker·섹터 + 시그널 pill. 근거·면책 없음."""
    kind_ko = _KIND_KO.get(kind, "관심")
    pill = _pill_color(kind)
    sec = _esc(sector or "")
    return (_svg_open(pill, bg)
        + f'<text x="120" y="560" fill="#9ca3af" font-size="52" font-weight="700">{_INTRO_KICKER}</text>'
        + f'<text x="120" y="720" fill="#ffffff" font-size="132" font-weight="800">{_esc(name)}</text>'
        + f'<text x="120" y="800" fill="#6b7280" font-size="44">{_esc(ticker)}{(" · " + sec) if sec else ""}</text>'
        + f'<rect x="120" y="900" rx="22" width="360" height="118" fill="{pill}"/>'
        + f'<text x="300" y="980" fill="#fff" font-size="58" font-weight="800" text-anchor="middle">{kind_ko}</text>'
        + f'<text x="540" y="980" fill="#e5e7eb" font-size="54" font-weight="700">종합 {score:+.2f}</text>'
        + '<text x="120" y="1180" fill="#9ca3af" font-size="46">왜 이 신호가 나왔을까?</text>'
        + '</svg>')


def _reason_svg(idx: int, total: int, reason: str, kind: str, bg: str | None = None) -> str:
    """근거 1개를 화면 가득 보여주는 프레임(순차 공개). 진행 도트 + 큰 문장. 면책 없음."""
    pill = _pill_color(kind)
    raw = str(reason or "")
    tag = raw.split("]", 1)[0][1:] if raw.startswith("[") else ""
    body = raw.split("] ", 1)[-1]
    tspans = "".join(f'<tspan x="120" dy="{0 if i == 0 else 116}">{_esc(l)}</tspan>'
                     for i, l in enumerate(_wrap(body, 13)))
    dots = "".join(f'<circle cx="{140 + i * 46}" cy="1740" r="15" '
                   f'fill="{pill if i == idx - 1 else "#374151"}"/>' for i in range(total))
    return (_svg_open(pill, bg)
        + f'<text x="120" y="360" fill="{pill}" font-size="48" font-weight="800">근거 {idx} / {total}</text>'
        + (f'<text x="120" y="440" fill="#9ca3af" font-size="40">{_esc(tag)}</text>' if tag else "")
        + f'<text x="120" y="720" fill="#ffffff" font-size="82" font-weight="700">{tspans}</text>'
        + dots + '</svg>')


def _scene_narration(i: int, body: str, name: str) -> str:
    """장면별 나레이션(음성 합성 입력). 0=인트로, 그 외=근거를 순차로 '말하는' 문장."""
    if i == 0:
        return f"오늘 주목할 종목, {name}입니다. 신호가 왜 떴는지 근거를 하나씩 볼게요."
    return f"{_CONNECT[min(i - 1, len(_CONNECT) - 1)]}, {body}."


def _scenes_for(name: str, ticker: str, kind: str, score: float,
                reasons: list[str], sector: str | None) -> list[dict]:
    """카드를 '장면 시퀀스'로 — 인트로(썸네일) → 근거별 프레임(순차 공개). 각 장면에 나레이션·길이.
    이 표현이 typecast(장면별 음성)든 자체 렌더(장면별 프레임)든 그대로 투입되는 중간 포맷."""
    clean = _reason_clean(reasons, 4)
    bg = _load_bg()  # 관리자 설정 배경(있으면 모든 프레임 공통 배경 + scrim)
    scenes = [{"label": "인트로", "dur": 3.0, "svg": _intro_svg(name, ticker, kind, score, sector, bg),
               "narration": _scene_narration(0, "", name)}]
    for i, r in enumerate(clean, 1):
        body = r.split("] ", 1)[-1]
        scenes.append({"label": f"근거 {i}", "dur": 4.0, "svg": _reason_svg(i, len(clean), r, kind, bg),
                       "narration": _scene_narration(i, body, name)})
    return scenes


def _full_caption(name: str, ticker: str, kind: str, reasons: list[str], llm_caption: str | None) -> str:
    """게시물 캡션 — 근거를 포함한 종합 해설 + (LLM 서술) + 투자유의(면책). 카드 프레임엔 면책을 넣지 않는다."""
    kind_ko = _KIND_KO.get(kind, "관심")
    bodies = [r.split("] ", 1)[-1] for r in _reason_clean(reasons, 4)]
    summary = f"📊 {name}({ticker}) {kind_ko} 시그널"
    if bodies:
        summary += " — 근거: " + " · ".join(bodies)
    parts = [summary]
    extra = (llm_caption or "").replace(_DISCLAIMER, "").replace("※", "").strip(" \n·")
    if extra and extra not in summary:
        parts.append(extra)
    parts.append(f"※ {_DISCLAIMER}")
    return "\n\n".join(parts)


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
        scenes = _scenes_for(s.name, s.ticker, s.kind, s.score, s.reasons, sector)  # 인트로+근거 순차 프레임
        caption = _full_caption(s.name, s.ticker, s.kind, s.reasons, sc.get("caption"))  # 근거 종합+면책
        item = {"id": uuid.uuid4().hex, "ticker": s.ticker, "name": s.name, "kind": s.kind,
                "score": round(s.score, 2), "title": sc["title"], "script": sc["script"],
                "caption": caption, "hashtags": sc["hashtags"],
                "card_svg": scenes[0]["svg"], "scenes": scenes, "status": "draft"}  # card_svg=인트로(썸네일)
        if not dry_run:
            db.shortform_add(item)
        made.append({k: item[k] for k in ("id", "ticker", "name", "kind", "score", "title")})
    log.info("숏폼 초안 생성: %d건", len(made))
    return {"ok": True, "created": made, "count": len(made)}


# ---------- 성과(track record) 숏폼 — 봇의 실제 성과를 콘텐츠로(봇↔숏폼 시너지) ----------
_PERF_DISCLAIMER = ("모의투자(페이퍼) 성과이며 과거 성과가 미래 수익을 보장하지 않습니다. "
                    "정보 제공용이며 투자 권유가 아닙니다.")


def _perf_card_svg(label: str, ret_pct, days: int, mdd, n_trades: int, unit: str, bg: str | None = None) -> str:
    """세로 1080x1920 성과 카드 — 성향·수익률·기간·최대낙폭·거래수."""
    color = "#22c55e" if (ret_pct or 0) >= 0 else "#ef4444"
    ret_s = f"{ret_pct:+.1f}%" if ret_pct is not None else "–"
    # 면책은 카드에 넣지 않고 캡션으로 옮긴다(신호 카드와 동일 정책).
    return (_svg_open(color, bg)
        + '<text x="120" y="220" fill="#9ca3af" font-size="44">공용 자동매매 봇 성과</text>'
        + f'<text x="120" y="340" fill="#ffffff" font-size="86" font-weight="800">{_esc(label)}</text>'
        + f'<text x="120" y="640" fill="#9ca3af" font-size="52">최근 {days}일 수익률</text>'
        + f'<text x="120" y="800" fill="{color}" font-size="200" font-weight="800">{ret_s}</text>'
        + f'<text x="120" y="1000" fill="#e5e7eb" font-size="48">최대낙폭 '
          f'{_esc(f"{mdd:.1f}%" if mdd is not None else "–")} · 거래 {n_trades}건</text>'
        + '</svg>')


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
    perf_svg = _perf_card_svg(label, ret, days, mdd, n, unit, bg=_load_bg())
    ret_line = f"최근 {days}일 수익률 {ret:+.1f}%" if ret is not None else f"최근 {days}일 성과"
    caption = f"📈 {label} 자동매매 봇 · {ret_line} · 최대낙폭 {mdd:.1f}% · 거래 {n}건\n\n※ {_PERF_DISCLAIMER}"
    scenes = [{"label": "성과", "dur": 5.0, "svg": perf_svg,
               "narration": f"공용 {label} 봇의 {ret_line}, 최대낙폭 {mdd:.1f}%, 거래 {n}건입니다."}]
    item = {"id": uuid.uuid4().hex, "ticker": "_PERF", "name": f"{label} 봇", "kind": "PERF",
            "score": ret, "title": title, "script": script, "caption": caption,
            "hashtags": ["#자동매매", "#퀀트", "#주식", f"#{label}", "#수익률공개"],
            "card_svg": perf_svg, "scenes": scenes, "status": "draft"}
    if not dry_run:
        db.shortform_add(item)
    return {"ok": True, "count": 1, "created": [{k: item[k] for k in ("id", "ticker", "name", "kind", "score", "title")}]}
