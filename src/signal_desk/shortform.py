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
from signal_desk.signals import engine, indicators as ind, macro, regime

log = logging.getLogger("signal_desk.shortform")

# 규제상 '매수/추천' 같은 단정 표현 대신 '주목' 톤으로 — 정보 제공·관심 환기 뉘앙스(투자권유 아님).
# 매수가 아닌 후보(HOLD 등)도 '관심'이 아니라 '주목'으로 — 뱃지에서 '관심'은 쓰지 않는다.
_KIND_KO = {"STRONG_BUY": "강한 주목", "BUY": "주목"}
_KIND_DEFAULT = "주목"


def _kind_label(kind: str) -> str:
    return _KIND_KO.get(kind, _KIND_DEFAULT)


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
    kind_ko = _kind_label(kind)
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
    if bg:  # 배경 사진(호스팅 URL) 위에 그라데이션 scrim — 밝고 복잡한 사진도 글자 가독성 유지.
        # 텍스트가 얹히는 좌·하단을 진하게(0.95), 배경 여백(우상단)은 살짝(0.55) — 분위기 유지 + 가독성.
        gid = "scr" + uuid.uuid4().hex[:8]  # 프레임마다 고유 id(같은 문서 내 여러 SVG id 충돌 방지)
        base = (f'<image href="{_esc(bg)}" xlink:href="{_esc(bg)}" x="0" y="0" width="1080" height="1920" '
                f'preserveAspectRatio="xMidYMid slice"/>'
                f'<defs><linearGradient id="{gid}" x1="0" y1="0.1" x2="0.9" y2="1">'
                f'<stop offset="0" stop-color="#0b1220" stop-opacity="0.6"/>'
                f'<stop offset="0.55" stop-color="#0b1220" stop-opacity="0.82"/>'
                f'<stop offset="1" stop-color="#0b1220" stop-opacity="0.95"/></linearGradient></defs>'
                f'<rect width="1080" height="1920" fill="url(#{gid})"/>')
    else:
        base = '<rect width="1080" height="1920" fill="#0b1220"/>'
    return head + base + f'<rect x="0" y="0" width="1080" height="12" fill="{bar}"/>'


def _linechart(vals, x: int, y: int, w: int, h: int, color: str, area: bool = True) -> str:
    """숫자 시퀀스 → SVG 라인(+면적) 경로. (x,y,w,h) 박스에 정규화. 2점 미만이면 빈 문자열.
    차트를 웹 스크린샷하지 않고 같은 데이터로 카드에 직접 그린다(해상도·스케일 자유)."""
    pts = [float(v) for v in (vals or []) if isinstance(v, (int, float))]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1.0
    n = len(pts)
    coords = [(x + i / (n - 1) * w, y + h - (p - lo) / rng * h) for i, p in enumerate(pts)]
    line = " ".join(f"{px:.0f},{py:.0f}" for px, py in coords)
    out = ""
    if area:
        out += f'<polygon points="{x:.0f},{y + h:.0f} {line} {x + w:.0f},{y + h:.0f}" fill="{color}" opacity="0.16"/>'
    out += f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="6" stroke-linejoin="round"/>'
    lx, ly = coords[-1]
    out += f'<circle cx="{lx:.0f}" cy="{ly:.0f}" r="12" fill="{color}"/>'  # 최신점 강조
    return out


def _intro_svg(name: str, ticker: str, kind: str, score: float, sector: str | None,
               bg: str | None = None) -> str:
    """인트로/썸네일 템플릿(재사용) — '오늘의 시그널' + 종목명 + ticker·섹터 + 시그널 pill. 근거·면책 없음."""
    kind_ko = _kind_label(kind)
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


# 정량(지표) 근거 태그 — 이 외(정성/KB)는 정성 장면으로 분리.
_QUANT_TAGS = ("기술", "기본", "저평가", "고평가", "낙폭과대", "단기과열", "수급", "퀄리티", "모멘텀", "추세")


def _tag(reason: str) -> str:
    r = str(reason or "")
    return r.split("]", 1)[0][1:] if r.startswith("[") else ""


def _won(v) -> str:
    try:
        return "₩" + f"{round(float(v)):,}"
    except (TypeError, ValueError):
        return "–"


def _big_won(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    if v >= 1e12:
        return f"{v / 1e12:.1f}조원"
    if v >= 1e8:
        return f"{v / 1e8:,.0f}억원"
    return f"{v:,.0f}원"


def _kicker(num: str, title: str, color: str) -> str:
    """장면 상단 공통 헤더 — 번호 + 제목."""
    return (f'<text x="120" y="360" fill="{color}" font-size="46" font-weight="800">{num}</text>'
            f'<text x="120" y="450" fill="#e5e7eb" font-size="60" font-weight="700">{_esc(title)}</text>')


def _bullets(items, color: str, y0: int = 780, gap: int = 118, max_n: int = 3, fs: int = 52) -> str:
    """근거·포인트 불릿 목록(정량/정성 장면 공용). tag 제거, 한 줄로 컷."""
    out = ""
    for i, it in enumerate([x for x in (items or []) if x][:max_n]):
        body = str(it).split("] ", 1)[-1]
        if len(body) > 24:
            body = body[:23] + "…"
        y = y0 + i * gap
        out += (f'<circle cx="138" cy="{y - 16}" r="9" fill="{color}"/>'
                f'<text x="180" y="{y}" fill="#e5e7eb" font-size="{fs}" font-weight="600">{_esc(body)}</text>')
    return out


# 정량 지표 → 초보도 이해되는 한 줄 뜻(근거 문구에서 키워드 매칭, 첫 매칭 사용).
_METRIC_HELP = [
    ("골든크로스", "단기 이평선이 장기선을 위로 뚫어 상승 전환 신호"),
    ("데드크로스", "단기선이 장기선 아래로 — 하락 전환 신호"),
    ("정배열", "이평선이 상승 순서로 정렬돼 추세가 양호"),
    ("과매도", "단기 낙폭이 커 반등 여지가 있는 구간"),
    ("과매수", "단기 급등으로 과열된 구간"),
    ("낙폭과대", "단기 급락으로 저가 매력이 생긴 구간"),
    ("저평가", "이익·자산 대비 주가가 업종에서 싼 편"),
    ("PER", "이익 대비 주가 수준 — 낮을수록 저평가"),
    ("PBR", "순자산 대비 주가 수준 — 낮을수록 저평가"),
    ("수급", "외국인·기관 등 큰손 자금이 들어오는 중"),
    ("외국인", "외국인 자금이 순매수하는 중"),
    ("기관", "기관 자금이 순매수하는 중"),
    ("순매수", "큰손 자금이 들어오는 중"),
    ("모멘텀", "최근 상승 추세의 힘이 살아있음"),
    ("퀄리티", "재무 건전성·수익성이 개선되는 중"),
    ("F-Score", "재무 건전성 점수가 양호"),
    ("부채", "빚 부담 수준(낮을수록 안정적)"),
    ("매출", "외형(매출)이 늘어나는 성장세"),
    ("ROE", "자기자본 대비 수익성"),
    ("거래량", "거래가 활발해지는 중"),
]


def _metric_gloss(text: str) -> str:
    """근거 문구의 핵심 지표를 한 줄 뜻으로. 매칭 없으면 빈 문자열."""
    t = str(text or "")
    for kw, meaning in _METRIC_HELP:
        if kw in t:
            return meaning
    return ""


def _company_desc(ticker: str, name: str, sector: str | None) -> str:
    """'무엇을 하는 회사인지' 한 문장(초보용). 공용 company 모듈에 위임(시그널 상세와 캐시 공유).
    투자권유·전망·수치 없이 사업 소개만. LLM 없으면 섹터 폴백."""
    from signal_desk import company
    return company.about(ticker, name, sector, market="kr", generate=True) \
        or (f"{sector} 분야의 기업입니다." if sector else "국내 상장 기업입니다.")


def _company_svg(name, ticker, sector, price, change, mktcap, per, bg=None, profile=None, desc=None) -> str:
    """① 기업 개요 — 종목명(+영문) · 무엇을 하는 회사인지 설명 · 섹터·설립·대표·현재가·시총·PER."""
    c = "#4f46e5"
    profile = profile or {}
    eng = profile.get("name_eng")
    # 사업 설명(무엇을 하는 회사) — 최대 3줄
    desc_svg, y = "", 720
    if eng:
        desc_svg += f'<text x="120" y="640" fill="#6b7280" font-size="40">{_esc(eng)}</text>'
    desc_svg += f'<text x="120" y="{700 if eng else 640}" fill="#6b7280" font-size="42">{_esc(ticker)}</text>'
    if desc:
        for j, ln in enumerate(_wrap(desc, 18)[:3]):
            desc_svg += f'<text x="120" y="{800 + j * 62}" fill="#cbd5e1" font-size="46">{_esc(ln)}</text>'
        y = 800 + min(len(_wrap(desc, 18)), 3) * 62 + 60
    rows = []
    if sector:
        rows.append(("섹터", sector))
    if profile.get("est_year"):
        rows.append(("설립", f"{profile['est_year']}년"))
    if profile.get("ceo"):
        rows.append(("대표", profile["ceo"].split(",")[0].strip()))  # 공동대표는 첫 명만
    if price:
        rows.append(("현재가", _won(price) + (f"  ({change:+.1f}%)" if change is not None else "")))
    if mktcap:
        rows.append(("시가총액", _big_won(mktcap)))
    if per and per > 0:
        rows.append(("PER", f"{per:.1f}배"))
    body = ""
    for i, (k, v) in enumerate(rows[:6]):
        yy = y + i * 128
        body += (f'<text x="120" y="{yy}" fill="#9ca3af" font-size="44">{_esc(k)}</text>'
                 f'<text x="960" y="{yy}" fill="#ffffff" font-size="50" font-weight="700" text-anchor="end">{_esc(str(v))}</text>')
    return (_svg_open(c, bg) + _kicker("01", "기업 개요", c)
        + f'<text x="120" y="560" fill="#ffffff" font-size="88" font-weight="800">{_esc(name)}</text>'
        + desc_svg + body + '</svg>')


def _fin_chips(m: dict) -> list[str]:
    """재무·밸류 지표 칩(DART 재무) — PER·PBR·ROE·매출성장. 있는 것만."""
    chips = []
    if m.get("per") and m["per"] > 0:
        chips.append(f"PER {m['per']:.1f}")
    if m.get("pbr") and m["pbr"] > 0:
        chips.append(f"PBR {m['pbr']:.1f}")
    if m.get("roe") is not None:
        chips.append(f"ROE {m['roe']:.0f}%")
    if m.get("revenue_growth") is not None:
        chips.append(f"매출 {m['revenue_growth']:+.0f}%")
    return chips


def _price_ma_chart(closes, x, y, w, h, color) -> str:
    """최근 1개월 주가(면적+라인) + 5일 이동평균선(점선) — 공유 스케일 오버레이. '지표를 보여주는' 차트.
    2점 미만이면 빈 문자열."""
    pts = [float(v) for v in (closes or []) if isinstance(v, (int, float))]
    if len(pts) < 2:
        return ""
    recent = pts[-22:]
    ma_full = ind.sma(pts, 5) if len(pts) >= 5 else []
    ma = ma_full[-len(recent):] if ma_full else []
    scale = recent + [m for m in ma if m is not None]
    lo, hi = min(scale), max(scale)
    rng = (hi - lo) or 1.0
    n = len(recent)

    def coords(series):
        out = []
        for i, v in enumerate(series):
            if v is None:
                continue
            out.append(f"{x + i / (n - 1) * w:.0f},{y + h - (v - lo) / rng * h:.0f}")
        return out

    price = coords(recent)
    svg = (f'<polygon points="{x:.0f},{y + h:.0f} {" ".join(price)} {x + w:.0f},{y + h:.0f}" '
           f'fill="{color}" opacity="0.16"/>'
           f'<polyline points="{" ".join(price)}" fill="none" stroke="{color}" stroke-width="6" stroke-linejoin="round"/>')
    ma_pts = coords(ma)
    if len(ma_pts) >= 2:
        svg += (f'<polyline points="{" ".join(ma_pts)}" fill="none" stroke="#eab308" '
                f'stroke-width="4" stroke-dasharray="12 7"/>')
        svg += ('<text x="960" y="{y}" fill="#eab308" font-size="30" text-anchor="end">┈ 5일선</text>'
                .replace("{y}", str(y - 8)))
    lx, ly = (x + w), (y + h - (recent[-1] - lo) / rng * h)
    svg += f'<circle cx="{lx:.0f}" cy="{ly:.0f}" r="10" fill="{color}"/>'
    return svg


def _quant_svg(quant_reasons, closes, kind, bg=None, metrics=None) -> str:
    """② 정량 지표 근거 — 근거마다 '지표 + 쉬운 뜻 한 줄' + 재무·밸류 칩 + 최근 1개월 주가 차트.
    지표만 나열하지 않고 의미를 붙여 이해가 쉽게."""
    pill = _pill_color(kind)
    body, y = "", 600
    for r in (quant_reasons or [])[:3]:
        rb = str(r).split("] ", 1)[-1]
        main = rb if len(rb) <= 20 else rb[:19] + "…"
        gloss = _metric_gloss(rb)
        body += (f'<circle cx="138" cy="{y - 18}" r="9" fill="{pill}"/>'
                 f'<text x="180" y="{y}" fill="#ffffff" font-size="54" font-weight="700">{_esc(main)}</text>')
        if gloss:
            body += f'<text x="180" y="{y + 50}" fill="#9ca3af" font-size="36">{_esc(gloss[:27])}</text>'
        y += 150 if gloss else 110
    chips = _fin_chips(metrics or {})
    chip_svg, cx = "", 120
    for ch in chips:
        w = 30 + len(ch) * 22
        if cx + w > 1000:  # 1080 폭 초과 방지
            break
        chip_svg += (f'<rect x="{cx}" y="1160" rx="16" width="{w}" height="72" fill="#1f2937"/>'
                     f'<text x="{cx + w // 2}" y="1208" fill="#e5e7eb" font-size="36" '
                     f'font-weight="700" text-anchor="middle">{_esc(ch)}</text>')
        cx += w + 16
    chart = _price_ma_chart(closes, 120, 1390, 840, 280, pill)
    label = '<text x="120" y="1340" fill="#9ca3af" font-size="40">최근 1개월 주가 + 5일선</text>' if chart else ""
    return (_svg_open(pill, bg) + _kicker("02", "정량 지표 근거", pill)
        + body + chip_svg + label + chart + '</svg>')


def _qual_svg(summary, points, kind, bg=None) -> str:
    """③ 정성 근거 — 뉴스·시황·섹터 호재(KB 다이제스트 요약 + 포인트). 없으면 안내."""
    c = "#0ea5e9"
    lines = ""
    if summary:
        sw = _wrap(summary, 15)[:3]
        lines = ('<text x="120" y="700" fill="#ffffff" font-size="58" font-weight="700">'
                 + "".join(f'<tspan x="120" dy="{0 if i == 0 else 84}">{_esc(l)}</tspan>' for i, l in enumerate(sw))
                 + '</text>')
    pts = _bullets(points, c, y0=1120, gap=110, max_n=3, fs=48)
    empty = ("" if (summary or points)
             else '<text x="120" y="720" fill="#9ca3af" font-size="52" font-weight="600">'
                  '최근 특별한 뉴스·시황 이슈는 크지 않아<tspan x="120" dy="80">지표 위주로 판단했습니다.</tspan></text>')
    return (_svg_open(c, bg) + _kicker("03", "정성 근거 · 뉴스·시황", c) + lines + pts + empty + '</svg>')


def _reco_svg(name, kind, easy_line, target, bg=None) -> str:
    """④ 주목 포인트 — 관심 강도(주목/강한 주목) + 쉬운 한줄 해설 + 참고 목표가(있으면). 투자권유 아님."""
    pill = _pill_color(kind)
    kind_ko = _kind_label(kind)
    ew = _wrap(easy_line, 14)[:3]
    etext = "".join(f'<tspan x="120" dy="{0 if i == 0 else 100}">{_esc(l)}</tspan>' for i, l in enumerate(ew))
    tgt = ""
    if target and target.get("value_target"):
        up = target.get("value_upside_pct")
        tgt = ('<text x="120" y="1480" fill="#9ca3af" font-size="44">참고 목표가</text>'
               f'<text x="120" y="1580" fill="#e5e7eb" font-size="66" font-weight="700">{_won(target["value_target"])}'
               + (f'  ({up:+.1f}%)' if up is not None else '') + '</text>'
               '<text x="120" y="1640" fill="#6b7280" font-size="32">※ PER 회귀 기준 참고치 · 목표·보장 아님</text>')
    return (_svg_open(pill, bg) + _kicker("04", "주목 포인트", pill)
        + f'<rect x="120" y="580" rx="20" width="320" height="104" fill="{pill}"/>'
        + f'<text x="280" y="652" fill="#fff" font-size="52" font-weight="800" text-anchor="middle">{kind_ko}</text>'
        + f'<text x="120" y="900" fill="#ffffff" font-size="76" font-weight="700">{etext}</text>'
        + tgt + '</svg>')


def _outro_svg(label: str, ret_pct, curve: list | None, bg: str | None = None) -> str:
    """아웃트로 — 우리 시그널을 따르는 레퍼런스 봇의 모의투자 누적 수익률 + 자산곡선 차트.
    '이 신호들이 실제로 이렇게 됐다'를 track record로 마무리(봇↔숏폼 시너지)."""
    color = "#22c55e" if (ret_pct or 0) >= 0 else "#ef4444"
    ret_s = f"{ret_pct:+.1f}%" if ret_pct is not None else "–"
    vals = [c.get("total_eval") for c in (curve or []) if c.get("total_eval") is not None]
    chart = _linechart(vals, 120, 1180, 840, 460, color)
    return (_svg_open(color, bg)
        + '<text x="120" y="360" fill="#9ca3af" font-size="46" font-weight="700">우리 시그널 봇 성적표</text>'
        + f'<text x="120" y="500" fill="#ffffff" font-size="72" font-weight="800">{_esc(label)}</text>'
        + '<text x="120" y="700" fill="#9ca3af" font-size="48">모의투자 누적 수익률</text>'
        + f'<text x="120" y="880" fill="{color}" font-size="168" font-weight="800">{ret_s}</text>'
        + chart
        + '<text x="120" y="1740" fill="#6b7280" font-size="34">※ 모의투자(페이퍼) 성과 · 미래 수익 보장 아님</text>'
        + '</svg>')


def _dur_for(text: str) -> float:
    """나레이션 길이로 장면 길이(초) 추정 — 한국어 ~5.5자/초 + 여유 0.6초, 최소 2.5초.
    실제 렌더 땐 합성된 오디오 길이로 대체(ffmpeg). 여기선 미리보기·대본 타이밍 근사."""
    n = len((text or "").strip())
    return round(max(2.5, n / 5.5 + 0.6), 1)


def _scene(label, narration, svg):
    return {"label": label, "narration": narration, "dur": _dur_for(narration), "svg": svg}


def _scenes_for(name: str, ticker: str, kind: str, score: float, reasons: list[str], sector: str | None,
                closes: list | None = None, quote: dict | None = None, kb: dict | None = None,
                target: dict | None = None, easy_line: str | None = None,
                outro: dict | None = None, profile: dict | None = None, desc: str | None = None) -> list[dict]:
    """고정 6장면 템플릿 — 0 인트로 · 1 기업개요 · 2 정량근거(차트) · 3 정성근거(뉴스·시황) ·
    4 주목 포인트(쉬운 해설·관심 강도·목표가) · 5 아웃트로(봇 수익률). 각 장면에 나레이션·길이.
    typecast(장면별 음성)든 자체 렌더(장면별 프레임)든 그대로 투입되는 중간 포맷."""
    bg = _load_bg()
    kind_ko = _kind_label(kind)
    quote, kb = quote or {}, kb or {}
    price = closes[-1] if closes else None
    change = (round((closes[-1] / closes[-2] - 1) * 100, 2)
              if closes and len(closes) >= 2 and closes[-2] else None)
    mktcap, per = quote.get("mktcap"), quote.get("per")
    quant = [r for r in (reasons or []) if _tag(r) in _QUANT_TAGS]
    qbodies = [r.split("] ", 1)[-1] for r in quant[:3]]
    summary, points = kb.get("summary"), (kb.get("points") or [])
    easy_line = easy_line or f"{name}는 지금 '{kind_ko}' 구간으로 평가됩니다."
    scenes = []
    # 0 인트로
    scenes.append(_scene("0·인트로",
        f"오늘 주목할 종목은 {name}입니다. 어떤 기업이고 왜 신호가 떴는지 순서대로 보겠습니다.",
        _intro_svg(name, ticker, kind, score, sector, bg)))
    # 1 기업 개요 — 무엇을 하는 회사인지 설명 포함
    prof = profile or {}
    est = f" {prof['est_year']}년 설립." if prof.get("est_year") else ""
    n1 = (f"{name}는 " + (desc + " " if desc else f"{sector or '해당 섹터'} 종목입니다. ") + est
          + (f" 현재가는 {_won(price)} 수준입니다." if price else ""))
    scenes.append(_scene("1·기업 개요",
        n1, _company_svg(name, ticker, sector, price, change, mktcap, per, bg, profile=prof, desc=desc)))
    # 2 정량 근거 — 지표 + 쉬운 뜻 + 재무 + 차트
    gloss = [g for g in (_metric_gloss(b) for b in qbodies) if g][:2]
    n2 = ("정량 지표를 보면, "
          + (", ".join(qbodies) + " 신호가 있습니다. " if qbodies else "기술·재무 지표를 종합했습니다. ")
          + ("쉽게 말하면, " + " 그리고 ".join(gloss) + "는 뜻입니다." if gloss else ""))
    scenes.append(_scene("2·정량 근거", n2, _quant_svg(quant, closes, kind, bg, metrics=quote)))
    # 3 정성 근거
    n3 = ("정성적으로는, " + (summary or "; ".join(points[:2])) if (summary or points)
          else "최근 특별한 뉴스나 시황 이슈는 크지 않아, 지표 위주로 판단했습니다.")
    scenes.append(_scene("3·정성 근거", n3, _qual_svg(summary, points, kind, bg)))
    # 4 주목 포인트
    n4 = easy_line + (f" 참고 목표가는 {_won(target['value_target'])} 수준입니다."
                      if target and target.get("value_target") else "")
    scenes.append(_scene("4·주목 포인트", n4, _reco_svg(name, kind, easy_line, target, bg)))
    # 5 아웃트로(봇 track record 있을 때만)
    if outro and outro.get("ret_pct") is not None:
        ret = outro["ret_pct"]
        scenes.append(_scene("5·아웃트로",
            f"참고로 우리 시그널을 따르는 {outro.get('label','봇')}은 모의투자 기준 누적 "
            f"{ret:+.1f}% 성과를 기록했습니다. 지금까지 오늘의 시그널이었습니다.",
            _outro_svg(outro.get("label", "봇"), ret, outro.get("curve"), bg)))
    return scenes


def _full_caption(name: str, ticker: str, kind: str, reasons: list[str], llm_caption: str | None) -> str:
    """게시물 캡션 — 근거를 포함한 종합 해설 + (LLM 서술) + 투자유의(면책). 카드 프레임엔 면책을 넣지 않는다."""
    kind_ko = _kind_label(kind)
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


def _reference_outro(style: str = "balanced", market: str = "kr") -> dict | None:
    """아웃트로용 레퍼런스 봇 track record — 누적 수익률 + 자산곡선. 성과 데이터 없으면 None(초기엔 스킵).
    모든 신호 카드에 공통으로 붙는 '우리 봇 성적표' 마무리."""
    try:
        from signal_desk import bot, strategy
        uid = {v: k for k, v in bot.REFERENCE_BOTS.items()}.get(style)
        if not uid:
            return None
        perf = bot.performance(uid, market)
        curve = db.bot_equity_curve(uid, market)
        if not perf.get("days") or perf.get("return_pct") is None or len(curve) < 2:
            return None
        return {"label": f"{strategy.STYLE_LABEL.get(style, style)} 봇",
                "ret_pct": perf["return_pct"], "curve": curve}
    except Exception as e:
        log.warning("아웃트로(봇 성과) 준비 실패(무시): %s", type(e).__name__)
        return None


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
    from signal_desk.signals import target as target_mod
    prices = store.load_price_series()  # 정량 근거 가격차트용(종목별 종가 시계열)
    funds = store.load_fundamentals()   # DART 재무: PER·PBR·ROE·매출성장·시총(기업개요·정량·목표가)
    profiles = store.load_company_profiles()  # DART 기업개황: 설립·대표·영문명(기업 개요)
    med_per = target_mod.median_per(funds)  # 목표가(PER 회귀) 기준 중앙값
    outro = _reference_outro()          # 아웃트로: 레퍼런스 봇 track record(모든 카드 공통, 없으면 None)
    made = []
    for s in picks:
        sector = sectors.sector_of(s.ticker)
        closes = prices.get(s.ticker)
        f = funds.get(s.ticker) or {}
        quote = {"mktcap": f.get("mktcap"), "per": f.get("per"), "pbr": f.get("pbr"),
                 "roe": f.get("roe"), "revenue_growth": f.get("revenue_growth")}
        kb_dg = db.kb_digest_get(s.ticker) or {}  # 정성 근거(뉴스·시황 다이제스트)
        price = closes[-1] if closes else None
        tgt = target_mod.compute(price, f.get("per"), med_per, closes)  # 참고 목표가(없으면 None)
        qbody = next((r.split("] ", 1)[-1] for r in s.reasons if _tag(r) in _QUANT_TAGS), "")
        easy = f"{s.name}는 {qbody + ' 등으로 ' if qbody else ''}{_kind_label(s.kind)} 신호가 나왔습니다."
        desc = _company_desc(s.ticker, s.name, sector)  # 무엇을 하는 회사(LLM 캐시)
        sc = _script_for(s.name, s.ticker, s.kind, s.reasons)
        scenes = _scenes_for(s.name, s.ticker, s.kind, s.score, s.reasons, sector,
                             closes=closes, quote=quote, kb=kb_dg, target=tgt, easy_line=easy,
                             outro=outro, profile=profiles.get(s.ticker), desc=desc)  # 고정 6장면 템플릿
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
