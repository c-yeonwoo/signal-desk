"""지식베이스(KB) — 뉴스·영상 원자료를 '한 번 더 가공'(요약·감성)해 종목별 다이제스트로 적재.

흐름: ingest.news.collect(원자료) → db.kb_entry_add_many(원자료 보관) → build_digest(LLM 요약·감성)
→ db.kb_digest_set(가공 결과 보관). 다이제스트는 (1) 시그널의 정성적 팩터(signals/qualitative.py)와
(2) 봇 LLM 자문(signals/advisor.py)의 입력으로 재사용된다.

리소스 절약: 전 종목이 아니라 호출자가 넘긴 대상(보유·후보·관심종목)만 갱신한다.
LLM 미설정 시 규칙기반(키워드) 감성으로 폴백 — KB는 여전히 쌓인다.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
import time

from signal_desk import config, db, llm
from signal_desk.ingest import dart as ingest_dart
from signal_desk.ingest import news

log = logging.getLogger("signal_desk.kb")


def _source_slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", (s or "").strip())[:64] or "x"

_POS = ["상승", "급등", "호재", "최대", "돌파", "수주", "흑자", "개선", "성장", "신고가", "강세", "기대", "수혜"]
_NEG = ["하락", "급락", "악재", "부진", "적자", "감소", "우려", "리콜", "제재", "약세", "손실", "하향", "경고"]

# 매수 후보에서 제외(veto)할 '악재 이벤트' 키워드 — 명백한 하방 사건만(고정밀). 업종과 충돌하는
# 일반어(화재/사고/폭발/소송/파업 등, 예: 화재보험사)나 중립어(유상증자/감산)는 오탐이 많아 제외.
# 강도 2단계: critical=존폐·신뢰 붕괴(전량 청산), serious=실적·제재 충격(부분 청산). 매수 veto는 둘 다.
_EVENT_CRITICAL = [
    "횡령", "배임", "분식회계", "불성실공시", "상장폐지", "거래정지", "감사의견 거절", "관리종목 지정",
]
_EVENT_SERIOUS = [
    "압수수색", "검찰 기소", "구속영장", "과징금", "리콜 결정", "어닝쇼크", "적자전환", "영업정지",
]
_EVENT_TERMS = _EVENT_CRITICAL + _EVENT_SERIOUS
EVENT_TTL_DAYS = 5  # 이 기간 지난 악재는 veto에서 해제(신선도)

# 시맨틱 veto용 프로토타입 — 키워드 동의어·완곡 표현. 점수 팩터가 아니라 악재 후보만.
# (라벨, 강도, 표현들). 임베딩 백엔드가 hashing이면 공유 토큰이 있을 때만 의미 있게 매칭.
_EVENT_PROTOTYPES: list[tuple[str, str, list[str]]] = [
    ("횡령", "critical", ["횡령", "회사 자금 유용", "법인카드 유용", "비자금 조성", "공금 횡령"]),
    ("배임", "critical", ["배임", "업무상 배임", "회사 재산 손괴", "배임 혐의"]),
    ("분식회계", "critical", ["분식회계", "회계 조작", "재무제표 허위", "회계부정"]),
    ("거래정지", "critical", ["거래정지", "매매거래 정지", "거래 중단"]),
    ("상장폐지", "critical", ["상장폐지", "상장 적격성 실질심사", "상장폐지 결정"]),
    ("감사의견 거절", "critical", ["감사의견 거절", "감사의견 부적정", "의견거절"]),
    ("압수수색", "serious", ["압수수색", "검찰 압수 수색", "수사 착수 압수수색"]),
    ("과징금", "serious", ["과징금", "공정위 과징금", "금감원 제재금"]),
    ("어닝쇼크", "serious", ["어닝쇼크", "실적 쇼크", "시장 예상 크게 하회", "영업이익 급감 충격"]),
    ("적자전환", "serious", ["적자전환", "영업적자 전환", "적자로 돌아섬"]),
    ("영업정지", "serious", ["영업정지", "영업 활동 정지", "업무정지 처분"]),
]

# DART 공시 전용 키워드 — 공시는 구조화·공신력 있어 뉴스보다 확실(뉴스 본문 오탐 없이 source=='dart'에만 매칭).
_DISC_CRITICAL = ["감자", "상장폐지", "상장적격성", "감사의견 거절", "감사의견 부적정", "회생절차", "부도", "파산"]
_DISC_SERIOUS = ["유상증자", "전환사채", "신주인수권부사채", "최대주주 변경", "공급계약 해지", "소송 등의 제기"]
# 호재/주목 공시(veto 아님, KB 근거로 적재) — 자기주식·무상증자·수주·흑자전환 등
_DISC_GOOD = ["자기주식 취득", "자기주식취득", "무상증자", "공급계약 체결", "공급계약체결",
              "수주", "흑자전환", "자산재평가", "현금·현물배당", "주식배당", "자기주식취득 신탁"]
_DISC_NOTABLE = _DISC_CRITICAL + _DISC_SERIOUS + _DISC_GOOD


def event_severity(note: str) -> str:
    """event_note 선두 키워드로 악재 강도 판정. critical|serious|''."""
    head = (note or "").split(" — ", 1)[0].strip()
    term = head.split("(", 1)[0].strip()
    if term in _EVENT_CRITICAL or term in _DISC_CRITICAL:
        return "critical"
    if term in _EVENT_SERIOUS or term in _DISC_SERIOUS:
        return "serious"
    for label, sev, _ in _EVENT_PROTOTYPES:
        if term == label:
            return sev
    return ""

# 거시·시황 내러티브 전용 가상 종목 — 개별 종목 KB와 격리(sentiment_map 등에서 '_' 접두 티커 제외).
# 시장 흐름 트래킹 + 봇 자문 컨텍스트로만 쓰이고, 개별 종목 시그널엔 섞이지 않는다(이중계상 방지).
MACRO_TICKER = "_MARKET"
MACRO_NAME = "시장 시황"

# 외부 소스(미주은·오건영·유튜브) 수집 하한 연도 — 그 이전 콘텐츠는 시황·거시 가치 낮아 스킵.
INGEST_MIN_YEAR = 2026


def _year_ok(published: str | None) -> bool:
    """발행일이 INGEST_MIN_YEAR 이상이면 True. 날짜 불명(빈값·비표준)은 포함(True)."""
    s = (published or "").strip()
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4]) >= INGEST_MIN_YEAR
    return True


# 문서 유형 분류 — 규칙 기반(투명·무료). 우선순위 순으로 첫 매칭 채택.
DOC_CLASSES = ("리포트", "공시", "실적", "이벤트", "시황", "뉴스")
_CLASS_RULES = [
    ("리포트", ["목표주가", "투자의견", "매수의견", "커버리지", "리포트", "적정주가", "투자등급"]),
    ("공시", ["공시", "정정공시", "공급계약", "단일판매", "자기주식", "주주총회", "유상증자", "무상증자"]),
    ("실적", ["실적", "영업이익", "잠정실적", "어닝", "컨센서스", "매출액", "당기순이익"]),
    ("이벤트", None),  # _EVENT_TERMS 사용(아래에서 주입)
    ("시황", ["코스피", "코스닥", "증시", "환율", "금리", "fomc", "국제유가", "거시", "나스닥"]),
]


def classify_document(item: dict, source_type: str | None = None) -> str:
    """문서를 유형으로 분류. source_type이 명시되면(report/insight/disclosure) 우선. 아니면 키워드 규칙."""
    if source_type == "report":
        return "리포트"
    if source_type == "insight":
        return "전문가인사이트"
    if source_type == "disclosure":
        return "공시"
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    for cls, terms in _CLASS_RULES:
        terms = _EVENT_TERMS if cls == "이벤트" else terms
        if any(term.lower() in text for term in terms):
            return cls
    return "뉴스"


_TRUST_ACCEPT = 0.7  # 이상이면 confirmed(다이제스트 반영)
_TRUST_INSIGHT_ACCEPT = 0.5  # 신뢰 출처(전문가 인사이트)는 완화된 accept 임계값
_TRUST_REVIEW = 0.4  # 미만이면 reject(미저장), 사이면 pending(보류)


def validate_import(ticker: str, name: str, text: str, title: str = "", trusted: bool = False) -> dict:
    """수동 입력 문서의 신뢰성 심사(KB 오염 방지). 반환: {verdict: accept|review|reject, trust, reasons}.

    1) 규칙 선필터(무료·빠름): 길이·증권 관련성·종목 언급.
    2) LLM 판단기(있으면 확정): 기존 confirmed KB와 대조해 과장·허위·조작·스팸·무관·근거없는 급변을
       탐지하고 신뢰도(trust)를 매긴다. 규칙과 LLM 중 더 보수적인 판정을 채택한다.

    trusted=True(큐레이션된 신뢰 출처, 예: 전문가 인사이트)면 accept 임계값을 낮춰(0.5) 의견·서사형
    콘텐츠가 pending에만 머무르지 않고 반영되게 한다. 단 명백한 오염(LLM reject·trust<REVIEW)은 그대로 차단."""
    accept_bar = _TRUST_INSIGHT_ACCEPT if trusted else _TRUST_ACCEPT
    body = f"{title} {text}"
    if len(text.strip()) < 40:
        return {"verdict": "reject", "trust": 0.0, "reasons": ["본문이 너무 짧아 신뢰 불가(40자 미만)"]}
    reasons = []
    if not any(term in body for term in news.SECURITIES_TERMS):
        reasons.append("증권 관련 키워드 없음")
    if name not in body and ticker not in body:
        reasons.append("종목명·코드 언급 없음(무관/오분류 의심)")
    rule_verdict = "review" if reasons else "accept"

    if llm.available():
        prior = (db.kb_digest_get(ticker) or {}).get("summary") or "(없음)"
        system = ("너는 주식 지식베이스(KB)의 품질 관리자다. 사용자가 수동 입력한 문서가 해당 종목의 "
                  "신뢰할 만한 증권 정보인지 보수적으로 심사한다. 과장·허위·조작·스팸·광고·무관·근거 없는 주장, "
                  "그리고 기존 KB 요약과 근거 없이 크게 모순·급변시키는지 본다.")
        user = (f"종목: {name}({ticker})\n[기존 KB 요약] {prior}\n[입력 문서]\n{text[:4000]}\n\n"
                'JSON으로만: {"trust": 0.0~1.0(신뢰도), "on_topic": true/false, '
                '"issues": ["의심 사유 짧게"], "verdict": "accept|review|reject"}')
        out = llm.complete_json(system, user, max_tokens=400)
        if out and isinstance(out.get("trust"), (int, float)):
            trust = max(0.0, min(1.0, float(out["trust"])))
            issues = [str(i) for i in (out.get("issues") or [])][:4]
            llm_v = str(out.get("verdict", "")).lower()
            # 신뢰도 임계값 기반 판정(신뢰 출처는 accept_bar 완화). 단 LLM이 명시적으로 reject하면 존중(오염 차단).
            v = "accept" if trust >= accept_bar else "review" if trust >= _TRUST_REVIEW else "reject"
            if llm_v == "reject":
                v = "reject"
            if rule_verdict == "review" and v == "accept":  # 규칙 의심이면 accept로 격상 금지(보수적)
                v = "review"
            return {"verdict": v, "trust": round(trust, 2), "reasons": reasons + issues}
    # LLM 없음 → 규칙 결과(중립 신뢰도)
    return {"verdict": rule_verdict, "trust": 0.5, "reasons": reasons}


def import_document(ticker: str, name: str, title: str, text: str,
                    source_type: str = "report", url: str = "", published: str = "",
                    *, source_key: str | None = None, display_name: str | None = None,
                    parent_key: str | None = None) -> dict:
    """증권사 리포트·원문 텍스트 → 신뢰성 검증 → 통과분만 KB 반영. 반환: {ok, status, doc_class, summary, trust, reasons}.
    accept=confirmed(시그널 반영) · review=pending(보류, 미반영) · reject=미저장. published=발행일(freshness)."""
    text = (text or "").strip()
    if not text or not ticker or not name:
        return {"ok": False, "reason": "ticker·name·text 필요"}
    v = validate_import(ticker, name, text, title, trusted=(source_type == "insight"))
    if v["verdict"] == "reject":
        return {"ok": False, "verdict": "reject", "trust": v["trust"], "reasons": v["reasons"],
                "reason": "KB 오염 우려로 저장하지 않음: " + (", ".join(v["reasons"]) or "신뢰도 낮음")}
    status = "confirmed" if v["verdict"] == "accept" else "pending"
    doc_class = classify_document({"title": title, "summary": text[:500]}, source_type)
    summary, points = _summarize_text(name, title, text)
    sk = source_key or ("fanding" if source_type == "insight" else "manual")
    parent = parent_key or (sk.split(":", 1)[0] if ":" in sk else None)
    gate = ingest_document(
        source_key=sk, ticker=ticker, title=title or f"{name} {source_type}",
        summary=summary, url=url, published=published, doc_class=doc_class,
        raw_text=text, status=status, scope="stock", entry_source=source_type,
        display_name=display_name, parent_key=parent if parent and parent != sk else None,
    )
    if not gate.get("ok"):
        return {"ok": False, "reason": gate.get("reason", "소스 게이트 거절"),
                "verdict": v["verdict"], "trust": v["trust"], "reasons": v["reasons"]}
    if status == "confirmed":
        _rebuild_digest(ticker, name)  # confirmed만 다이제스트에 반영
    return {"ok": True, "status": status, "verdict": v["verdict"], "trust": v["trust"],
            "reasons": v["reasons"], "doc_class": doc_class, "summary": summary,
            "source_key": sk}


def _summarize_text(name: str, title: str, text: str) -> tuple[str, list[str]]:
    """긴 원문(리포트 등) → 투자관점 요약 1~2문장 + 핵심 포인트. LLM 없으면 앞부분 발췌."""
    if llm.available():
        system = ("너는 한국 주식 애널리스트다. 아래 문서를 투자 관점에서 사실 기반으로 요약한다. "
                  "과장·추천 금지, 문서에 없는 내용 금지.")
        user = (f"종목: {name}\n제목: {title}\n본문:\n{text[:6000]}\n\n"
                'JSON으로만: {"summary": "한국어 1~2문장", "points": ["핵심 ≤3개"]}')
        out = llm.complete_json(system, user, max_tokens=500, model=llm.DIGEST_QUALITY_MODEL)
        if out and out.get("summary"):
            return str(out["summary"])[:300], [str(p) for p in (out.get("points") or [])][:3]
    excerpt = " ".join(text.split())[:200]
    return f"{name} 문서 발췌: {excerpt}", []


_MIN_PDF_TEXT = 200  # 이보다 짧으면 '스캔/이미지 PDF'로 보고 vision(OCR)으로 폴백


def _pdf_text(data: bytes) -> str:
    """네이티브 텍스트 PDF에서 본문 추출(pypdf). 스캔본이면 거의 빈 문자열이 나온다."""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as e:
        log.warning("PDF 텍스트 추출 실패: %s", type(e).__name__)
        return ""


def _summarize_vision(name: str, title: str, data: bytes, media_type: str) -> tuple[str, list[str]]:
    """스캔 PDF·이미지를 모델이 직접 읽어 요약(OCR 대체). 실패 시 빈 요약."""
    import base64
    if not llm.available():
        return "", []
    system = ("너는 한국 주식 애널리스트다. 첨부된 문서/이미지의 내용을 읽고 투자 관점에서 사실 기반으로 "
              "요약한다. 과장·추천 금지, 문서에 없는 내용 금지.")
    user = (f"종목: {name} / 제목: {title}\n첨부 문서를 요약해줘.\n"
            'JSON으로만: {"summary": "한국어 1~2문장", "points": ["핵심 ≤3개"]}')
    out = llm.complete_json_vision(system, user, media_type=media_type,
                                   data_b64=base64.b64encode(data).decode("ascii"))
    if out and out.get("summary"):
        return str(out["summary"])[:300], [str(p) for p in (out.get("points") or [])][:3]
    return "", []


def _classify_scope(text: str) -> dict:
    """문서 스코프 자동 판정 — 특정 종목(stock)/시황(market)/섹터(sector) 중 무엇인가.
    반환 {scope, ticker, name, sector}. ticker는 코스피 유니버스에 실재하는 것만 채택(환각 차단)."""
    from signal_desk import store
    uni = store.load_universe()
    by_name = {u["name"]: u["ticker"] for u in uni}
    tk_to_name = {u["ticker"]: u["name"] for u in uni}
    if not llm.available() or not text.strip():
        return {"scope": "market", "ticker": None, "name": None, "sector": None}  # 폴백: 시황
    system = ("너는 증권 문서 분류기다. 문서가 (1) 특정 상장사 한 곳 분석이면 stock, "
              "(2) 거시·시황·시장 전반이면 market, (3) 특정 산업/섹터 전망이면 sector로 분류한다.")
    user = (f"문서:\n{text[:4000]}\n\n"
            'JSON으로만: {"scope":"stock|market|sector", "company":"회사명(stock일 때만, 아니면 null)", '
            '"ticker":"6자리 코드(알면, 아니면 null)", "sector":"섹터명(sector일 때만, 아니면 null)"}')
    out = llm.complete_json(system, user, max_tokens=200, model=llm.CLASSIFY_MODEL) or {}
    scope = str(out.get("scope") or "market").lower()
    tk, nm = out.get("ticker"), out.get("company")
    if tk not in tk_to_name:                     # 코드 환각 방지 — 유니버스에 없으면 회사명으로 재매핑
        tk = by_name.get(nm) if nm else None
    if scope == "stock" and not tk:              # 종목 특정 실패 → 시황으로 안전 강등
        scope = "market"
    return {"scope": scope, "ticker": tk, "name": tk_to_name.get(tk) if tk else None,
            "sector": out.get("sector")}


def validate_macro(text: str, title: str = "") -> dict:
    """시황·섹터 문서 안전망 — 증권·거시로서 신뢰할 콘텐츠인지(광고·스팸·무관·허위 차단). accept|reject."""
    if len((text or "").strip()) < 40:
        return {"verdict": "reject", "reasons": ["본문이 너무 짧음"]}
    if not llm.available():
        return {"verdict": "accept" if any(t in text for t in news.SECURITIES_TERMS) else "review", "reasons": []}
    system = ("너는 KB 품질관리자다. 이 문서가 시황·거시·섹터 분석으로서 신뢰할 증권 콘텐츠인지 심사한다. "
              "광고·스팸·무관·허위·근거 없는 주장은 reject.")
    user = (f"제목:{title}\n문서:\n{text[:4000]}\n\n"
            'JSON으로만: {"verdict":"accept|reject","reasons":["짧게"]}')
    # 종목 validate_import와 동일 — 오염 게이트는 Opus
    out = llm.complete_json(system, user, max_tokens=200, model=llm.DEFAULT_MODEL) or {}
    v = str(out.get("verdict", "accept")).lower()
    return {"verdict": "reject" if v == "reject" else "accept",
            "reasons": [str(r) for r in (out.get("reasons") or [])][:3]}


# ---------- P1 ingest gate (source registry) ----------
def _resolve_source(source_key: str, *, display_name: str | None = None,
                    parent_key: str | None = None) -> dict | None:
    src = db.kb_source_get(source_key)
    if src:
        return src
    if parent_key:
        return db.kb_source_ensure(source_key, display_name=display_name, parent_key=parent_key)
    return None


def _source_allows(src: dict | None, scope: str) -> tuple[bool, str]:
    if not src:
        return False, "등록되지 않은 소스"
    if not src.get("enabled"):
        return False, f"비활성 소스({src.get('source_key')})"
    scopes = src.get("allowed_scopes") or []
    if scopes and scope not in scopes:
        return False, f"scope '{scope}' 미허용"
    return True, ""


def _legacy_store_source(family: str, *, entry_source: str | None = None) -> str:
    """kb_entries.source 레거시 값 — prune/증분 URL 집합 호환."""
    if entry_source:
        return entry_source
    if family in ("youtube", "rss", "fanding", "outstanding"):
        return "insight"
    if family == "manual":
        return "report"
    return family


def ingest_document(*, source_key: str, ticker: str, title: str, summary: str,
                    url: str = "", published: str = "", doc_class: str = "",
                    raw_text: str | None = None, status: str = "confirmed",
                    scope: str = "stock", parent_key: str | None = None,
                    display_name: str | None = None,
                    entry_source: str | None = None) -> dict:
    """공통 적재 게이트 — registry 활성·scope 확인 후 저장. Decision 이벤트는 만들지 않음."""
    src = _resolve_source(source_key, display_name=display_name, parent_key=parent_key)
    ok, why = _source_allows(src, scope)
    touch_key = source_key if src else (parent_key or source_key)
    if not ok:
        if db.kb_source_get(touch_key):
            db.kb_sources_touch(touch_key, "rejected", error=why, rejected=1)
        return {"ok": False, "reason": why, "source_key": source_key}
    leg = _legacy_store_source(src["source_family"], entry_source=entry_source)
    dclass = doc_class or src.get("default_doc_class") or "뉴스"
    eid = db.kb_document_add(
        ticker, title, summary, url, leg, published, dclass,
        raw_text=raw_text, status=status,
    )
    db.kb_sources_touch(
        source_key, "ok",
        accepted=1 if status == "confirmed" else 0,
        pending=1 if status == "pending" else 0,
    )
    return {"ok": True, "entry_id": eid, "status": status, "source_key": source_key,
            "trust_tier": src.get("trust_tier")}


def ingest_stock_batch(ticker: str, items: list[dict]) -> int:
    """종목 뉴스·공시 배치 — item.source(dart|naver_news|…)별 registry 게이트 후 add_many.
    레거시 digest veto(detect_event)는 유지. Decision eligible 이벤트는 DART sync만."""
    allowed, counts = [], {}
    for it in items:
        raw_src = (it.get("source") or "naver_news").strip()
        sk = "dart" if raw_src == "dart" else "naver_news"
        src = db.kb_source_get(sk)
        ok, why = _source_allows(src, "stock")
        if not ok:
            if src:
                db.kb_sources_touch(sk, "rejected", error=why, rejected=1)
            continue
        row = {**it, "source": "dart" if sk == "dart" else (raw_src or "naver_news")}
        allowed.append(row)
        counts[sk] = counts.get(sk, 0) + 1
    n = db.kb_entry_add_many(ticker, allowed) if allowed else 0
    for sk, cnt in counts.items():
        db.kb_sources_touch(sk, "ok", accepted=cnt)
    return n


def import_file(ticker: str | None, name: str, filename: str, data: bytes, media_type: str) -> dict:
    """업로드 파일(PDF/이미지)을 KB 문서로. 텍스트 PDF는 pypdf로 싸게, 스캔·이미지는 vision(OCR)으로 인식.
    ticker가 없으면 문서 내용을 이해해 종목/시황/섹터로 자동 분류·라우팅한다(종목 특정 시 종목 KB, 아니면
    거시 KB). 검증 안전망은 두 경로 모두 유지. 반환: {ok, doc_class, summary, method, routed, ticker, name}."""
    if not data:
        return {"ok": False, "reason": "파일 필요"}
    disp = name or "문서"
    title = filename or f"{disp} 업로드"
    text, method = "", ""
    if media_type == "application/pdf":
        text = _pdf_text(data)
    if len(text) >= _MIN_PDF_TEXT:
        summary, _ = _summarize_text(disp, title, text)
        raw, method = text, "pdf_text"
    else:  # 스캔 PDF 또는 이미지 → 모델이 직접 인식(OCR)
        summary, _ = _summarize_vision(disp, title, data, media_type)
        raw, method = "[스캔/이미지 문서 — 모델 인식]", "vision"
        if not summary:
            return {"ok": False, "reason": "문서 인식 실패(LLM 키 확인 또는 지원 형식인지 확인)"}
    basis = text if method == "pdf_text" else summary  # 검증·분류에 쓸 본문

    # 종목 미지정 → 자동 스코프 분류(종목/시황/섹터)
    if not ticker:
        sc = _classify_scope(basis)
        if sc["scope"] == "stock" and sc["ticker"]:
            ticker, name = sc["ticker"], sc["name"]   # 종목 KB 경로로 계속(아래)
        else:
            vm = validate_macro(basis, title)          # 시황/섹터 안전망
            if vm["verdict"] == "reject":
                return {"ok": False, "verdict": "reject", "method": method, "routed": sc["scope"],
                        "reason": "KB 오염 우려로 저장하지 않음: " + (", ".join(vm["reasons"]) or "신뢰도 낮음")}
            is_sector = sc["scope"] == "sector" and sc.get("sector")
            label = (f"[섹터: {sc['sector']}] {title}" if is_sector else f"[시황] {title}")
            gate = ingest_document(
                source_key="manual", ticker=MACRO_TICKER, title=label, summary=summary,
                url="", published="", doc_class="시황", raw_text=raw, status="confirmed",
                scope="market", entry_source="upload",
            )
            if not gate.get("ok"):
                return {"ok": False, "reason": gate.get("reason"), "method": method}
            _rebuild_macro_digest()
            return {"ok": True, "status": "confirmed", "method": method,
                    "routed": "sector" if is_sector else "market",
                    "sector": sc.get("sector"), "doc_class": "시황", "summary": summary}

    # 종목 KB 경로(명시 ticker 또는 자동 감지) — 검증 안전망 유지
    if not name:
        return {"ok": False, "reason": "종목명을 찾지 못했습니다(코드만으로는 검증 불가)"}
    v = validate_import(ticker, name, basis, title)
    if v["verdict"] == "reject":
        return {"ok": False, "verdict": "reject", "trust": v["trust"], "reasons": v["reasons"], "method": method,
                "reason": "KB 오염 우려로 저장하지 않음: " + (", ".join(v["reasons"]) or "신뢰도 낮음")}
    status = "confirmed" if v["verdict"] == "accept" else "pending"
    doc_class = classify_document({"title": title, "summary": summary}, "report")
    gate = ingest_document(
        source_key="manual", ticker=ticker, title=title, summary=summary,
        url="", published="", doc_class=doc_class, raw_text=raw, status=status,
        scope="stock", entry_source="upload",
    )
    if not gate.get("ok"):
        return {"ok": False, "reason": gate.get("reason"), "method": method,
                "verdict": v["verdict"], "trust": v["trust"]}
    if status == "confirmed":
        _rebuild_digest(ticker, name)
    return {"ok": True, "status": status, "verdict": v["verdict"], "trust": v["trust"],
            "reasons": v["reasons"], "doc_class": doc_class, "summary": summary, "method": method,
            "routed": "stock", "ticker": ticker, "name": name}


def _rebuild_digest(ticker: str, name: str) -> None:
    """해당 종목의 최근 confirmed KB 문서(뉴스+리포트)를 합쳐 다이제스트·이벤트·신선도를 재계산.
    pending(검토 보류) 문서는 제외해 시그널에 반영되지 않게 한다(오염 방지)."""
    items = db.kb_entries_recent(ticker, 15, confirmed_only=True)
    if not items:
        return
    digest = build_digest(name, items)
    event_flag, event_note = detect_event(items)
    db.kb_digest_set(ticker, name, digest["sentiment"], digest["summary"], digest["points"],
                     len(items), newest_ts=_newest_ts(items), event_flag=event_flag, event_note=event_note)


def detect_event(items: list[dict]) -> tuple[bool, str]:
    """원자료 제목/요약에서 악재 이벤트를 찾아 (플래그, 사유) 반환. 없으면 (False, "").
    1) 고정밀 키워드 2) 프로토타입 구문 확장 3) 시맨틱 cosine. 점수 팩터 아님(veto 전용)."""
    for it in items:
        text = f"{it.get('title', '')} {it.get('summary', '')}"
        terms = _EVENT_TERMS + (_DISC_CRITICAL + _DISC_SERIOUS if it.get("source") == "dart" else [])
        for term in terms:
            if term in text:
                return True, f"{term} — {(it.get('title') or '').strip()[:60]}"
        for label, _sev, phrases in _EVENT_PROTOTYPES:
            for ph in phrases:
                if len(ph) >= 4 and ph not in terms and ph in text:
                    return True, f"{label} — {(it.get('title') or '').strip()[:60]}"
    return _detect_event_semantic(items)


def _detect_event_semantic(items: list[dict]) -> tuple[bool, str]:
    """임베딩 cosine ≥ τ 이면 악재 후보. hashing은 공유 n-gram이 강할 때만(τ↑)."""
    try:
        from signal_desk import kb_embed
    except Exception:
        return False, ""
    texts, meta = [], []
    for it in items:
        t = f"{it.get('title', '')} {it.get('summary', '')}".strip()
        if t:
            texts.append(t)
            meta.append(it)
    if not texts:
        return False, ""
    try:
        doc_vecs = kb_embed.embed_texts(texts)
        proto_labels = [label for label, _sev, _ph in _EVENT_PROTOTYPES]
        proto_texts = [" · ".join(ph) for _l, _s, ph in _EVENT_PROTOTYPES]
        proto_vecs = kb_embed.embed_texts(proto_texts)
    except Exception:
        return False, ""
    tau = kb_embed.EVENT_SEMANTIC_TAU
    if not kb_embed.semantic_capable():
        tau = max(tau, 0.88)
    best = (0.0, "", "")
    for it, dv in zip(meta, doc_vecs):
        for label, pv in zip(proto_labels, proto_vecs):
            s = kb_embed.cosine(dv, pv)
            if s > best[0]:
                best = (s, label, (it.get("title") or "")[:60])
    if best[0] >= tau and best[1]:
        return True, f"{best[1]}(의미근접 {best[0]:.2f}) — {best[2]}"
    return False, ""


def _disclosure_items(corp_code: str | None) -> list[dict]:
    """최근(신선도 기간) DART 주요공시를 news-like item으로 — 악재/호재/주목 공시만 필터.
    build_digest·detect_event·이벤트 카드가 소비. 키/코드 없으면 []."""
    if not corp_code:
        return []
    from datetime import date, timedelta
    end = date.today()
    bgn = end - timedelta(days=EVENT_TTL_DAYS + 2)
    items = []
    for r in ingest_dart.disclosures(corp_code, bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")):
        nm = r["report_nm"]
        if not any(k in nm for k in _DISC_NOTABLE):  # 분기보고서·IR 등 routine은 스킵(노이즈 방지)
            continue
        d = r["rcept_dt"]
        published = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else ""
        items.append({"title": f"[공시] {nm}", "summary": "", "source": "dart", "published": published,
                      "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r['rcept_no']}",
                      "doc_class": "공시", "rcept_no": r.get("rcept_no") or ""})
    return items


def _classify_disclosure(report_nm: str) -> dict | None:
    """공시 제목 → 구조화 이벤트 메타. Decision eligible은 악재(critical/serious)만."""
    nm = (report_nm or "").strip()
    if not nm:
        return None
    for term in _DISC_CRITICAL:
        if term in nm:
            return {
                "event_type": "disclosure_critical", "matched": term,
                "direction": "negative", "severity": "critical",
                "decision_eligible": True, "decision_action": "exit",
            }
    for term in _DISC_SERIOUS:
        if term in nm:
            return {
                "event_type": "disclosure_serious", "matched": term,
                "direction": "unknown" if term in ("유상증자", "전환사채", "신주인수권부사채", "최대주주 변경") else "negative",
                "severity": "serious",
                "decision_eligible": True, "decision_action": "buy_block",
            }
    for term in _DISC_GOOD:
        if term in nm:
            return {
                "event_type": "disclosure_positive", "matched": term,
                "direction": "positive", "severity": "info",
                "decision_eligible": False, "decision_action": "attention",
            }
    return None


def sync_disclosure_events(ticker: str, items: list[dict]) -> int:
    """DART 공시 items → kb_events(+evidence). official tier · 근거 URL 필수. 저장 건수 반환."""
    n = 0
    now = int(time.time())
    ttl = EVENT_TTL_DAYS * 86400
    for it in items:
        if it.get("source") != "dart":
            continue
        title = (it.get("title") or "").replace("[공시] ", "", 1)
        meta = _classify_disclosure(title)
        if not meta:
            continue
        url = (it.get("url") or "").strip()
        if not url:
            continue  # 근거 없는 카드 금지
        rcept = (it.get("rcept_no") or "").strip()
        if not rcept and "rcpNo=" in url:
            rcept = url.split("rcpNo=", 1)[-1].split("&", 1)[0]
        event_key = f"dart:{rcept}" if rcept else f"dart:{ticker}:{meta['matched']}:{it.get('published') or ''}"
        published = it.get("published") or ""
        effective = None
        if len(published) >= 10 and published[4] == "-":
            try:
                effective = int(datetime.datetime.strptime(published[:10], "%Y-%m-%d").timestamp())
            except ValueError:
                effective = None
        detected = effective or now
        db.kb_event_upsert(
            {
                "event_key": event_key,
                "scope_type": "stock",
                "ticker": ticker,
                "event_type": meta["event_type"],
                "direction": meta["direction"],
                "severity": meta["severity"],
                "confidence": 1.0,
                "trust_tier": "official",
                "status": "confirmed",
                "decision_eligible": meta["decision_eligible"],
                "decision_action": meta["decision_action"],
                "detected_at": detected,
                "effective_at": effective,
                "expires_at": detected + ttl,
                "summary": f"{meta['matched']} — {title[:80]}",
                "rationale": "DART 공식 공시 키워드 매칭(P0)",
                "extractor_model": "rule:dart_p0",
                "policy_version": "p0",
            },
            evidence={
                "source_key": "dart",
                "url": url,
                "published": published,
                "evidence_text": title[:200],
                "support_role": "primary",
                "trust_score": 1.0,
            },
        )
        n += 1
    return n


# ---------- P1b: 비-DART Sonnet candidate 이벤트 (Decision eligible 아님) ----------
_CANDIDATE_EVENT_TYPES = frozenset({
    "earnings", "guidance", "contract", "litigation", "regulatory",
    "management", "capital_raise", "mna", "product", "other_material",
})
_CANDIDATE_DIRECTIONS = frozenset({"negative", "positive", "mixed", "unknown"})
_CANDIDATE_SEVERITIES = frozenset({"info", "watch", "serious", "critical"})
_CANDIDATE_MAX_PER_REFRESH = 3  # 종목당 Sonnet 호출 상한(비용)


def _candidate_event_key(source_key: str, url: str) -> str:
    h = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]
    return f"candidate:{source_key}:{h}"


def _news_source_key(item: dict) -> str:
    raw = (item.get("source") or "naver_news").strip()
    if raw == "dart":
        return "dart"
    if raw in ("naver_news", "news", ""):
        return "naver_news"
    return raw


def _extract_candidate_event(ticker: str, item: dict) -> dict | None:
    """비-DART 원문 1건 → 구조화 후보 메타. 물질적 이벤트 없으면 None.
    Decision/점수에 쓰지 않음 — 조사·관리 UI용. DIGEST_QUALITY_MODEL(Sonnet)."""
    url = (item.get("url") or "").strip()
    title = (item.get("title") or "").strip()
    if not url or not title:
        return None
    if not llm.available():
        return None
    summary = (item.get("summary") or "")[:400]
    system = (
        "너는 주식 KB 이벤트 추출기다. 헤드라인·요약에서 물질적 기업 이벤트만 구조화한다. "
        "루머·감성·일반 시황·중복 공시 재전송은 event=false. "
        "원문에 없는 사실·숫자를 지어내지 마라. "
        "악재(negative)는 근거가 있으면 우선 추출하고, 호재(positive)는 명확한 계약·실적·승인만. "
        "evidence_text는 제목/요약에서 확인 가능한 짧은 인용·패러프레이즈여야 한다."
    )
    user = (
        f"종목코드:{ticker}\n제목:{title}\n요약:{summary}\nURL:{url}\n\n"
        "JSON만:\n"
        '{"event":true|false,\n'
        ' "event_type":"earnings|guidance|contract|litigation|regulatory|management|'
        'capital_raise|mna|product|other_material",\n'
        ' "direction":"negative|positive|mixed|unknown",\n'
        ' "severity":"info|watch|serious|critical",\n'
        ' "confidence":0.0~1.0,\n'
        ' "summary":"한국어 한 줄",\n'
        ' "rationale":"왜 물질적인지 한 줄",\n'
        ' "evidence_text":"원문 근거 인용"}'
    )
    out = llm.complete_json(system, user, max_tokens=280, model=llm.DIGEST_QUALITY_MODEL) or {}
    if not out.get("event"):
        return None
    et = str(out.get("event_type") or "")
    direction = str(out.get("direction") or "unknown")
    severity = str(out.get("severity") or "info")
    evidence_text = str(out.get("evidence_text") or "").strip()
    if et not in _CANDIDATE_EVENT_TYPES or direction not in _CANDIDATE_DIRECTIONS:
        return None
    if severity not in _CANDIDATE_SEVERITIES or not evidence_text:
        return None
    try:
        conf = max(0.0, min(1.0, float(out.get("confidence") or 0.5)))
    except (TypeError, ValueError):
        conf = 0.5
    return {
        "event_type": et,
        "direction": direction,
        "severity": severity,
        "confidence": conf,
        "summary": str(out.get("summary") or title)[:160],
        "rationale": str(out.get("rationale") or "")[:200],
        "evidence_text": evidence_text[:240],
    }


def sync_candidate_events(ticker: str, items: list[dict]) -> int:
    """새로 들어온 비-DART 뉴스 → candidate 카드(+evidence). decision_eligible 항상 False.
    URL·근거 없으면 스킵. 종목당 상한. LLM 없거나 실패 시 0."""
    n = 0
    now = int(time.time())
    ttl = EVENT_TTL_DAYS * 86400
    for it in items:
        if n >= _CANDIDATE_MAX_PER_REFRESH:
            break
        if (it.get("source") or "") == "dart":
            continue
        url = (it.get("url") or "").strip()
        if not url:
            continue
        source_key = _news_source_key(it)
        event_key = _candidate_event_key(source_key, url)
        if db.kb_event_exists(event_key):
            continue
        src = db.kb_source_get(source_key) or db.kb_source_get("naver_news")
        if src and not src.get("enabled"):
            continue
        trust = (src or {}).get("trust_tier") or "medium"
        meta = _extract_candidate_event(ticker, it)
        if not meta:
            continue
        published = it.get("published") or ""
        effective = None
        if len(published) >= 10 and published[4] == "-":
            try:
                effective = int(datetime.datetime.strptime(published[:10], "%Y-%m-%d").timestamp())
            except ValueError:
                effective = None
        detected = effective or now
        db.kb_event_upsert(
            {
                "event_key": event_key,
                "scope_type": "stock",
                "ticker": ticker,
                "event_type": meta["event_type"],
                "direction": meta["direction"],
                "severity": meta["severity"],
                "confidence": meta["confidence"],
                "trust_tier": trust,
                "status": "candidate",
                "decision_eligible": False,
                "decision_action": "none",
                "detected_at": detected,
                "effective_at": effective,
                "expires_at": detected + ttl,
                "summary": meta["summary"],
                "rationale": meta["rationale"] or "Sonnet 비-DART 후보 추출(P1b)",
                "extractor_model": llm.DIGEST_QUALITY_MODEL,
                "policy_version": "p1b",
            },
            evidence={
                "source_key": source_key,
                "url": url,
                "published": published,
                "evidence_text": meta["evidence_text"],
                "support_role": "primary",
                "trust_score": meta["confidence"],
            },
        )
        n += 1
    return n


def _active_decision_event(ticker: str) -> dict | None:
    """Decision 입력용 최강 활성 이벤트(critical > serious). 없으면 None."""
    from signal_desk.signals import decision as decmod
    events = db.kb_events_active(ticker, decision_only=True)
    d = decmod.decide(events)
    if not d.event_id:
        return None
    return next((e for e in events if e.get("id") == d.event_id), None)


def _newest_ts(items: list[dict]) -> int | None:
    """원자료 중 가장 최근 발행 시각(epoch). 파싱 가능한 게 없으면 None."""
    times = [dt.timestamp() for it in items if (dt := news._parse_dt(it.get("published", "")))]
    return int(max(times)) if times else None


def _rule_digest(name: str, items: list[dict]) -> dict:
    """LLM 없을 때 폴백 — 제목 키워드로 감성 근사, 최근 제목을 포인트로."""
    text = " ".join(f"{it.get('title', '')} {it.get('summary', '')}" for it in items)
    pos = sum(text.count(w) for w in _POS)
    neg = sum(text.count(w) for w in _NEG)
    total = pos + neg
    sentiment = round((pos - neg) / total, 2) if total else 0.0
    points = [it["title"] for it in items[:3] if it.get("title")]
    summary = f"{name} 최근 뉴스 {len(items)}건 기준 키워드 감성 {sentiment:+.2f}(규칙기반)."
    return {"sentiment": sentiment, "summary": summary, "points": points}


def build_digest(name: str, items: list[dict]) -> dict:
    """원자료 → {sentiment[-1..1], summary, points[≤3]}. LLM 우선, 실패 시 규칙기반.
    단순 헤드라인 재서술이 아니라 실적·수요·비용·정책·수급 등 경제적 함의를 뽑는다."""
    if not items:
        return {"sentiment": 0.0, "summary": "최근 수집된 뉴스·영상이 없습니다.", "points": []}
    if llm.available():
        headlines = "\n".join(f"- [{it.get('source', '')}] {it.get('title', '')} :: {it.get('summary', '')[:120]}"
                              for it in items[:12])
        system = (
            "너는 주식 데스크의 정성 분석가다. 헤드라인을 그대로 줄여 쓰지 말고, "
            "주가·실적·밸류에이션에 영향 가능한 경제적 함의만 추출한다. "
            "볼 것: 수요/수주, 마진·비용, 실적·가이던스, 규제·정책, 경쟁·점유율, "
            "수급(외국인/기관)·이벤트리스크. "
            "헤드라인에 없는 숫자·전망은 지어내지 마라. 매수/매도 권유·수익률 보장 금지. "
            "잡음(인사·단순 시황 언급)은 무시하고 물질적 이슈만 남긴다."
        )
        user = (
            f"종목: {name}\n최근 헤드라인:\n{headlines}\n\n"
            "JSON으로만 답하라:\n"
            '{"sentiment": -1.0~1.0 (물질적 뉴스 기준 투자심리),\n'
            ' "summary": "한국어 1~2문장 — 무엇이 바뀌었고 왜 경제적으로 중요한지",\n'
            ' "points": ["핵심 포인트 최대 3개 — 동사+대상+함의 (짧게)"]}'
        )
        out = llm.complete_json(system, user, max_tokens=500, model=llm.DIGEST_QUALITY_MODEL)
        if out and isinstance(out.get("sentiment"), (int, float)):
            s = max(-1.0, min(1.0, float(out["sentiment"])))
            pts = [str(p) for p in (out.get("points") or [])][:3]
            return {"sentiment": round(s, 2), "summary": str(out.get("summary", ""))[:280], "points": pts}
        log.info("LLM 다이제스트 파싱 실패 — 규칙기반 폴백")
    return _rule_digest(name, items)


def import_macro(title: str, text: str, url: str = "", published: str = "", summary: str = "",
                 rebuild: bool = True, *, source_key: str = "rss",
                 display_name: str | None = None, parent_key: str | None = None) -> dict:
    """시황·거시 내러티브(단일 종목 특정 불가)를 거시 KB(_MARKET)에 적재한다.
    개별 종목 검증(종목명 언급)은 적용하지 않되, 너무 짧은 글은 배제. 저장 후 거시 다이제스트 갱신.
    summary가 주어지면 다이제스트용 요약으로 쓴다(긴 자막 등은 미리 LLM 요약해 넘김). text=원문(raw).
    rebuild=False면 다이제스트 재계산을 건너뛴다(다건 배치 수집 시 끝에 1회만 재계산하기 위함).
    source_key/parent_key로 P1 registry 게이트를 통과한다."""
    text = (text or "").strip()
    if len(text) < 40:
        return {"ok": False, "reason": "본문이 너무 짧아 시황 KB에 저장하지 않음"}
    parent = parent_key or (source_key.split(":", 1)[0] if ":" in source_key else source_key)
    gate = ingest_document(
        source_key=source_key, ticker=MACRO_TICKER, title=title or "시황",
        summary=(summary or text)[:400], url=url, published=published, doc_class="시황",
        raw_text=text, status="confirmed", scope="market",
        parent_key=parent if source_key != parent else None,
        display_name=display_name, entry_source="insight",
    )
    if not gate.get("ok"):
        return {"ok": False, "reason": gate.get("reason", "소스 게이트 거절")}
    if rebuild:
        _rebuild_macro_digest()
    return {"ok": True, "status": "confirmed", "doc_class": "시황", "source_key": source_key}


def _macro_source_summary(title: str, text: str) -> str:
    """긴 원문(자막 등)을 거시 KB 저장용 시장 관점 요약으로 압축. LLM 없거나 실패 시 앞부분 폴백."""
    text = text.strip()
    if len(text) <= 600 or not llm.available():
        return text[:600]
    system = ("너는 시황 데스크다. 아래 영상/글 스크립트를 '투자·시장 관점'에서 핵심만 요약한다. "
              "거시 흐름·자산시장 시사점 위주로, 과장·추천 없이 사실 기반. 스크립트에 없는 내용은 지어내지 마라.")
    user = (f"제목: {title}\n스크립트:\n{text[:9000]}\n\n"
            'JSON으로만: {"summary": "한국어 2~4문장 핵심 요약", "points": ["핵심 포인트 최대 3개 짧게"]}')
    out = llm.complete_json(system, user, max_tokens=500, model=llm.DIGEST_QUALITY_MODEL)
    if out and out.get("summary"):
        pts = [str(p) for p in (out.get("points") or [])][:3]
        return (str(out["summary"]) + (" · " + " · ".join(pts) if pts else ""))[:600]
    return text[:600]


def collect_youtube(max_per_channel: int | None = None, force: bool = False) -> dict:
    """유튜브 화이트리스트 채널의 최신 영상을 자막 전문 기반으로 거시 KB(_MARKET)에 적재.
    자막이 있으면 LLM으로 시장 관점 요약(다이제스트용) + 원문(raw) 보관, 없으면 설명으로 폴백.
    거시 중심(상장사 특정 영상만 종목 KB). 증분: 이미 적재된 URL 스킵.
    max_per_channel 미지정 시 config.youtube_max_per_channel() 사용(env로 조절)."""
    from signal_desk.ingest import youtube
    if not config.youtube_key():
        return {"ok": False, "reason": "YOUTUBE_API_KEY 미설정(.env) — 유튜브 수집 건너뜀"}
    if max_per_channel is None:
        max_per_channel = config.youtube_max_per_channel()
    seen = set() if force else db.kb_document_urls(source="insight")
    macro, skipped, errors = [], [], []
    # 화이트리스트 채널은 거시·시장 해설 전용 → 제목에 기업명이 있어도 항상 거시 KB로(개별 종목 경로 X).
    for handle in config.youtube_channels():
        res = youtube.channel_videos(handle, max_results=max_per_channel)
        channel = res.get("channel") or handle
        if not res.get("videos"):
            errors.append({"channel": handle, "why": "영상 목록 조회 실패"})
            continue
        for v in res["videos"]:
            title, vid = v.get("title") or "", v.get("video_id")
            url = youtube.video_url(vid)
            if url in seen:
                skipped.append({"video_id": vid, "title": title, "why": "이미 수집됨"})
                continue
            if not _year_ok(v.get("published")):
                skipped.append({"video_id": vid, "title": title, "why": f"{INGEST_MIN_YEAR} 이전(스킵)"})
                continue
            raw = youtube.transcript(vid) or (v.get("description") or "")
            if len(raw.strip()) < 60:
                skipped.append({"video_id": vid, "title": title, "why": "자막·설명 없음"})
                continue
            pub = v.get("published") or ""
            summary = _macro_source_summary(title, raw)  # 긴 자막은 LLM 요약, 원문은 raw 보관
            sk = f"youtube:{_source_slug(handle)}"
            r = import_macro(f"[{channel}] {title}", raw, url=url, published=pub, summary=summary,
                             source_key=sk, display_name=f"YouTube @{handle}", parent_key="youtube")
            if r.get("ok"):
                macro.append({"channel": channel, "title": title, "published": pub, "chars": len(raw)})
            else:
                skipped.append({"video_id": vid, "title": title, "why": r.get("reason", "미저장")})
    log.info("youtube 수집: 거시 %d · 스킵 %d · 오류 %d", len(macro), len(skipped), len(errors))
    return {"ok": True, "imported": [], "macro": macro, "skipped": skipped, "errors": errors}


def collect_rss_macro(force: bool = False, limit_per_feed: int | None = None) -> dict:
    """해외 전문가·기관 RSS 화이트리스트(config.macro_rss_feeds)의 최신 글을 거시 KB(_MARKET)에
    요약 적재. 국내 아마추어 소스 보완 — 검증된 고품질 시장·거시 논평만(의견=맥락, 신호 아님).
    영문 원문은 _macro_source_summary(LLM)가 한국어 시장관점 요약으로 압축. 증분: 이미 적재된 URL 스킵.
    다건이라 항목별 다이제스트 재계산은 생략하고 끝에 1회만."""
    from signal_desk.ingest import rss
    feeds = config.macro_rss_feeds()
    if not feeds:
        return {"ok": False, "reason": "MACRO_RSS_FEEDS 화이트리스트 없음"}
    limit = limit_per_feed or 5
    seen = set() if force else db.kb_document_urls(source="insight")
    macro, skipped, errors = [], [], []
    for feed in feeds:
        name, url = feed.get("name") or "RSS", feed.get("url")
        if not url:
            continue
        entries = rss.feed_entries(url, limit=limit)
        if not entries:
            errors.append({"feed": name, "why": "피드 조회 실패/빈 결과"})
            continue
        for e in entries:
            link, title = e.get("url") or "", e.get("title") or ""
            if link and link in seen:
                skipped.append({"title": title, "why": "이미 수집됨"})
                continue
            if not _year_ok(e.get("published")):
                skipped.append({"title": title, "why": f"{INGEST_MIN_YEAR} 이전(스킵)"})
                continue
            raw = (e.get("summary") or "").strip()
            if len(raw) < 40:
                skipped.append({"title": title, "why": "본문 짧음"})
                continue
            summary = _macro_source_summary(title, raw)  # 영문→한국어 시장관점 요약(LLM)
            sk = f"rss:{_source_slug(name)}"
            r = import_macro(f"[{name}] {title}", raw, url=link, published=e.get("published", ""),
                             summary=summary, rebuild=False,
                             source_key=sk, display_name=name, parent_key="rss")
            if r.get("ok"):
                macro.append({"feed": name, "title": title, "published": e.get("published", "")})
                if link:
                    seen.add(link)
            else:
                skipped.append({"title": title, "why": r.get("reason", "미저장")})
    if macro:
        _rebuild_macro_digest()  # 배치 끝에 1회만
    log.info("RSS 매크로 수집: 거시 %d · 스킵 %d · 오류 %d", len(macro), len(skipped), len(errors))
    return {"ok": True, "macro": macro, "skipped": skipped, "errors": errors}


def build_macro_digest(items: list[dict]) -> dict:
    """시황·거시 원문 여러 건 → 현재 '시장 톤' 내러티브 {summary(1~2문장), points[≤3]}.
    최신 글을 앞에 놓아 freshness를 반영(LLM엔 최신순으로 전달). LLM 없으면 최신 제목 나열."""
    if not items:
        return {"summary": "최근 수집된 시황 코멘터리가 없습니다.", "points": []}
    if llm.available():
        lines = "\n".join(f"- ({it.get('published', '')[:10]}) {it.get('title', '')} :: {(it.get('summary') or '')[:140]}"
                          for it in items[:10])
        system = ("너는 미국 증시 시황 데스크다. 아래는 최신순으로 정렬된 시장 해설·브리핑 모음이다. "
                  "이를 근거로 '지금 시장 톤'을 요약한다. 최신 글에 더 무게를 두고, 개별 종목 추천은 하지 마라. "
                  "제공된 내용에 없는 사실은 지어내지 마라.")
        user = (f"[최신순 시황 코멘터리]\n{lines}\n\n"
                'JSON으로만: {"summary": "한국어 1~2문장, 현재 시장 톤·핵심 이슈", '
                '"points": ["핵심 포인트 최대 3개(한국어 짧게)"]}')
        out = llm.complete_json(system, user, max_tokens=500, model=llm.DIGEST_QUALITY_MODEL)
        if out and out.get("summary"):
            pts = [str(p) for p in (out.get("points") or [])][:3]
            return {"summary": str(out["summary"])[:240], "points": pts}
    return {"summary": f"미주은 시황 코멘터리 {len(items)}건 수집(최신: {items[0].get('title', '')[:40]}).",
            "points": [it["title"] for it in items[:3] if it.get("title")]}


def _rebuild_macro_digest() -> None:
    """최근 confirmed 시황 문서를 합쳐 거시 다이제스트(내러티브)를 재계산·저장(_MARKET)."""
    items = db.kb_entries_recent(MACRO_TICKER, 12, confirmed_only=True)
    if not items:
        return
    dg = build_macro_digest(items)
    db.kb_digest_set(MACRO_TICKER, MACRO_NAME, 0.0, dg["summary"], dg["points"],
                     len(items), newest_ts=_newest_ts(items), event_flag=False, event_note="")


def macro_digest() -> dict | None:
    """거시 KB 내러티브 다이제스트 — 시황 전광판·봇 자문 컨텍스트가 소비. 없으면 None."""
    dg = db.kb_digest_get(MACRO_TICKER)
    if not dg or not dg.get("summary"):
        return None
    now = time.time()
    fresh = dg.get("newest_ts") is None or (now - dg["newest_ts"]) <= 10 * 86400  # 10일 내
    return {"summary": dg["summary"], "points": dg.get("points") or [],
            "count": dg.get("n_sources"), "newest_ts": dg.get("newest_ts"),
            "updated": dg.get("updated"), "fresh": fresh}


def refresh(targets: list[dict], news_n: int = 8, lookback_days: int = 7) -> dict:
    """targets: [{ticker, name}]. 각 종목 증권 뉴스 수집(신선도·관련성 필터)→저장→다이제스트 갱신.
    유튜브는 화이트리스트 확보 전까지 보류. 갱신 건수 반환."""
    updated = 0
    codes = ingest_dart.corp_codes()  # stock_code→corp_code(DART 공시 조회용, 1회). 키 없으면 {}
    for t in targets:
        ticker, name = t.get("ticker"), t.get("name", "")
        if not ticker or not name:
            continue
        news_items = news.collect(name, news_n=news_n, lookback_days=lookback_days)
        disc = _disclosure_items(codes.get(ticker))  # DART 주요공시(악재 veto·호재 근거) — 뉴스보다 확실
        items = disc + news_items
        if not items:
            continue
        for it in items:  # 문서 유형 분류(공시는 이미 지정됨 → 뉴스만 분류)
            if not it.get("doc_class"):
                it["doc_class"] = classify_document(it, "news")
        # P1b: 신규 비-DART만 Sonnet 후보 추출(재수집 URL은 스킵 — 비용)
        news_urls = [it["url"] for it in news_items if it.get("url")]
        already = db.kb_entry_urls_existing(news_urls)
        new_news = [it for it in news_items if it.get("url") and it["url"] not in already]
        ingest_stock_batch(ticker, items)  # P1: source registry 게이트
        sync_disclosure_events(ticker, disc)  # P0: 구조화 이벤트 카드(공식 공시)
        sync_candidate_events(ticker, new_news)  # P1b: candidate only · Decision 미반영
        digest = build_digest(name, items)
        # digest 플래그는 레거시·폴백 — Decision은 active event 우선(sentiment_map)
        event_flag, event_note = detect_event(items)
        active = _active_decision_event(ticker)
        if active:
            event_flag, event_note = True, active.get("summary") or event_note
        db.kb_digest_set(ticker, name, digest["sentiment"], digest["summary"], digest["points"],
                         len(items), newest_ts=_newest_ts(items), event_flag=event_flag, event_note=event_note)
        updated += 1
    pruned = db.kb_prune()  # 뉴스 무한 누적·만료 pending 정리(큐레이션 업로드는 보존)
    embedded = 0
    try:
        from signal_desk import kb_embed
        embedded = kb_embed.embed_missing(limit=120)  # entry_add_many 경로 증분 임베드
    except Exception:
        pass
    return {"updated": updated, "pruned": pruned, "embedded": embedded}


def sentiment_map() -> dict[str, dict]:
    """ticker -> {score, reasons, decision, event_*} — engine이 소비.
    Decision은 confirmed+decision_eligible 이벤트만(P2). 레거시 digest event_flag는
    참고용으로 남기고 매수 차단/청산에는 쓰지 않는다."""
    from signal_desk.signals import decision as decmod
    out = {}
    for ticker, dg in db.kb_digests_all().items():
        if ticker.startswith("_"):  # 거시·시황 등 가상 종목은 개별 시그널에 반영 안 함(격리)
            continue
        reasons = []
        if dg.get("summary"):
            reasons.append(f"[정성] {dg['summary']}")
        events = db.kb_events_active(ticker, decision_only=True)
        dec = decmod.decide(events)
        if dec.summary:
            reasons.append(f"[이벤트] {dec.summary}")
        out[ticker] = {
            "score": dg.get("sentiment", 0.0), "reasons": reasons,
            "event_risk": dec.buy_blocked,  # 별칭
            "event_note": dec.summary,
            "event_severity": dec.severity or "",
            "event_id": dec.event_id,
            "decision": dec,
        }
    return out


def _fanding_ticker_index() -> list[tuple[str, str, str]]:
    """미주은 포스트 제목에서 종목을 짚기 위한 (한글명, 티커, 영문표기) 인덱스.
    긴 이름 우선(부분일치 오탐 방지: '알파벳'이 'GOOG/GOOGL' 둘 다면 첫 매칭)."""
    from signal_desk.reference import us_ko
    idx = [(ko, tk, us_ko.name_ko(tk, ko)) for tk, ko in us_ko.NAME_KO.items()]
    idx.sort(key=lambda x: len(x[0]), reverse=True)
    return idx


# 순수 운영·홍보 공지(투자 정보 아님) — 시황 KB에도 넣지 않고 버린다.
_FANDING_NOISE = ("공지", "결제", "카드 등록", "회원권", "만화책", "질문 수집", "당첨", "이벤트 안내", "안내")


def _fanding_posts(fanding, backfill_days: int) -> list[dict]:
    """수집 대상 목록. backfill_days=0이면 최신 20건(일상 증분). >0이면 iLastPostNo 커서로
    그 일수 이전까지 페이지네이션(20건씩, 안전 상한 20페이지). cutoff 이전 글은 제외."""
    if not backfill_days:
        return fanding.post_list(limit=20)
    import datetime
    cutoff = (datetime.date.today() - datetime.timedelta(days=backfill_days)).isoformat()
    out: list[dict] = []
    before = None
    for _ in range(20):  # 최대 400건
        page = fanding.post_list(limit=20, before=before)
        if not page:
            break
        out.extend(page)
        oldest = (page[-1].get("published") or "")[:10]
        if oldest and oldest < cutoff:  # 이 페이지에서 cutoff 이전 도달 → 중단
            break
        before = page[-1].get("post_no")
    return [p for p in out if (p.get("published") or "")[:10] >= cutoff]  # cutoff 이후만


def collect_fanding(limit: int = 20, force: bool = False, backfill_days: int = 0) -> dict:
    """fanding.kr 미주은 포스트를 훑어 KB로 적재.
    - 종목 특정 글 → 종목 KB(전문가 인사이트, 검증기 게이트).
    - 종목 불특정이라도 시황·거시·시장흐름 해설 → 거시 KB(_MARKET, 시장흐름 트래킹·봇 자문용).
    - 순수 운영·홍보 공지(멤버십·결제·만화책 등)만 폐기.
    backfill_days=0(기본): 최신 20건 증분(일상). >0: 그 일수 이전까지 커서 페이징 백필(초기 1회용).
    증분 수집: 이미 적재된 URL은 본문 조회·LLM 요약 없이 건너뛴다(force=True면 전량 재수집)."""
    from signal_desk.ingest import fanding
    if not config.fanding_cookie():
        return {"ok": False, "reason": "FANDING_TT 미설정(.env) — 자동수집 건너뜀"}
    index = _fanding_ticker_index()
    seen = set() if force else db.kb_document_urls(source="insight")
    imported, macro, skipped, errors = [], [], [], []
    posts = _fanding_posts(fanding, backfill_days)
    if not posts:  # 쿠키는 있는데 목록이 비면 인증 만료·차단 가능성(빈 결과와 구분해 알림)
        return {"ok": False, "reason": "미주은 목록 조회 실패 — tt 세션 토큰 만료 가능. .env의 FANDING_TT 갱신 필요.",
                "imported": [], "macro": [], "skipped": [], "errors": []}
    for post in posts:
        title = post.get("title") or ""
        url = fanding.post_url(post.get("post_no"))
        if url in seen:
            skipped.append({"post_no": post.get("post_no"), "title": title, "why": "이미 수집됨"})
            continue
        if any(w in title for w in _FANDING_NOISE):
            skipped.append({"post_no": post.get("post_no"), "title": title, "why": "운영·홍보 공지(폐기)"})
            continue
        if not _year_ok(post.get("published")):
            skipped.append({"post_no": post.get("post_no"), "title": title, "why": f"{INGEST_MIN_YEAR} 이전(스킵)"})
            continue
        hit = next(((tk, ko, en) for ko, tk, en in index if ko in title), None)
        detail = fanding.post_detail(post["post_no"])
        if not detail:
            errors.append({"post_no": post.get("post_no"), "title": title, "why": "본문 조회 실패"})
            continue
        pub = detail.get("published") or ""
        if hit:
            tk, ko, en = hit
            res = import_document(tk, en, detail["title"], detail["content"],
                                  source_type="insight", url=detail["url"], published=pub,
                                  source_key="fanding", display_name="미주은(fanding)")
            if res.get("ok"):
                imported.append({"ticker": tk, "name": ko, "title": detail["title"],
                                 "status": res["status"], "trust": res.get("trust"), "published": pub})
            else:
                skipped.append({"post_no": post.get("post_no"), "title": title, "why": res.get("reason", "미저장")})
        else:  # 종목 불특정 → 시황·거시 내러티브로 적재
            res = import_macro(detail["title"], detail["content"], url=detail["url"], published=pub,
                               source_key="fanding", display_name="미주은(fanding)")
            if res.get("ok"):
                macro.append({"title": detail["title"], "published": pub})
            else:
                skipped.append({"post_no": post.get("post_no"), "title": title, "why": res.get("reason", "미저장")})
    log.info("fanding 수집: 종목 %d · 시황 %d · 스킵 %d · 오류 %d",
             len(imported), len(macro), len(skipped), len(errors))
    return {"ok": True, "imported": imported, "macro": macro, "skipped": skipped, "errors": errors}


def collect_outstanding(item_per_page: int = 50, force: bool = False) -> dict:
    """아웃스탠딩(outstanding.kr) 화이트리스트 작가의 최신 기고를 수집.
    콘텐츠가 대부분 거시·산업 해설이라 기본은 거시 KB(_MARKET), 상장사 특정 글만 종목 KB.
    공개 기고만 수집(유료글은 로그인 쿠키 없으면 건너뜀). 증분: 이미 적재된 URL 스킵."""
    from signal_desk.ingest import outstanding
    index = _fanding_ticker_index()
    has_cookie = bool(config.outstanding_cookie())
    seen = set() if force else db.kb_document_urls(source="insight")
    imported, macro, skipped, errors = [], [], [], []
    for login_id in config.outstanding_authors():
        res = outstanding.author_posts(login_id, item_per_page=item_per_page)
        author = (res.get("author") or {}).get("name") or login_id
        if not res.get("posts"):
            errors.append({"author": login_id, "why": "기고 목록 조회 실패(일시 오류일 수 있음 — 재시도 권장)"})
            continue
        for post in res["posts"]:
            title, uri = post.get("title") or "", post.get("uri") or ""
            url = outstanding.post_url(uri)
            if url in seen:
                skipped.append({"uri": uri, "title": title, "why": "이미 수집됨"})
                continue
            if post.get("is_private") and not has_cookie:
                skipped.append({"uri": uri, "title": title, "why": "유료글(로그인 쿠키 없음)"})
                continue
            if not _year_ok(post.get("datetime")):
                skipped.append({"uri": uri, "title": title, "why": f"{INGEST_MIN_YEAR} 이전(스킵)"})
                continue
            body = (post.get("body") or "").strip()
            if len(body) < 40:
                skipped.append({"uri": uri, "title": title, "why": "본문 없음/너무 짧음"})
                continue
            pub = post.get("datetime") or ""
            hit = next(((tk, ko, en) for ko, tk, en in index if ko in title), None)
            if hit:
                tk, ko, en = hit
                sk = f"outstanding:{_source_slug(login_id)}"
                r = import_document(tk, en, title, body, source_type="insight", url=url, published=pub,
                                    source_key=sk, display_name=f"아웃스탠딩 · {author}",
                                    parent_key="outstanding")
                (imported if r.get("ok") else skipped).append(
                    {"ticker": tk, "name": ko, "title": title, "why": r.get("reason")} if not r.get("ok")
                    else {"ticker": tk, "name": ko, "title": title, "status": r["status"], "published": pub})
            else:  # 거시·산업 내러티브 — 작가 attribution 유지
                sk = f"outstanding:{_source_slug(login_id)}"
                r = import_macro(f"[{author}] {title}", body, url=url, published=pub,
                                 source_key=sk, display_name=f"아웃스탠딩 · {author}",
                                 parent_key="outstanding")
                if r.get("ok"):
                    macro.append({"author": author, "title": title, "published": pub})
                else:
                    skipped.append({"uri": uri, "title": title, "why": r.get("reason", "미저장")})
    log.info("outstanding 수집: 종목 %d · 거시 %d · 스킵 %d · 오류 %d",
             len(imported), len(macro), len(skipped), len(errors))
    return {"ok": True, "imported": imported, "macro": macro, "skipped": skipped, "errors": errors}
