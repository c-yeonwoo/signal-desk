"""지식베이스(KB) — 뉴스·영상 원자료를 '한 번 더 가공'(요약·감성)해 종목별 다이제스트로 적재.

흐름: ingest.news.collect(원자료) → db.kb_entry_add_many(원자료 보관) → build_digest(LLM 요약·감성)
→ db.kb_digest_set(가공 결과 보관). 다이제스트는 (1) 시그널의 정성적 팩터(signals/qualitative.py)와
(2) 봇 LLM 자문(signals/advisor.py)의 입력으로 재사용된다.

리소스 절약: 전 종목이 아니라 호출자가 넘긴 대상(보유·후보·관심종목)만 갱신한다.
LLM 미설정 시 규칙기반(키워드) 감성으로 폴백 — KB는 여전히 쌓인다.
"""

from __future__ import annotations

import logging
import time

from signal_desk import config, db, llm
from signal_desk.ingest import news

log = logging.getLogger("signal_desk.kb")

_POS = ["상승", "급등", "호재", "최대", "돌파", "수주", "흑자", "개선", "성장", "신고가", "강세", "기대", "수혜"]
_NEG = ["하락", "급락", "악재", "부진", "적자", "감소", "우려", "리콜", "제재", "약세", "손실", "하향", "경고"]

# 매수 후보에서 제외(veto)할 '악재 이벤트' 키워드 — 명백한 하방 사건만(고정밀). 업종과 충돌하는
# 일반어(화재/사고/폭발/소송/파업 등, 예: 화재보험사)나 중립어(유상증자/감산)는 오탐이 많아 제외.
_EVENT_TERMS = [
    "횡령", "배임", "분식회계", "불성실공시", "상장폐지", "거래정지", "감사의견 거절", "관리종목 지정",
    "압수수색", "검찰 기소", "구속영장", "과징금", "리콜 결정", "어닝쇼크", "적자전환", "영업정지",
]
EVENT_TTL_DAYS = 5  # 이 기간 지난 악재는 veto에서 해제(신선도)

# 거시·시황 내러티브 전용 가상 종목 — 개별 종목 KB와 격리(sentiment_map 등에서 '_' 접두 티커 제외).
# 시장 흐름 트래킹 + 봇 자문 컨텍스트로만 쓰이고, 개별 종목 시그널엔 섞이지 않는다(이중계상 방지).
MACRO_TICKER = "_MARKET"
MACRO_NAME = "시장 시황"


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
                    source_type: str = "report", url: str = "", published: str = "") -> dict:
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
    db.kb_document_add(ticker, title or f"{name} {source_type}", summary, url,
                       source_type, published, doc_class, raw_text=text, status=status)
    if status == "confirmed":
        _rebuild_digest(ticker, name)  # confirmed만 다이제스트에 반영
    return {"ok": True, "status": status, "verdict": v["verdict"], "trust": v["trust"],
            "reasons": v["reasons"], "doc_class": doc_class, "summary": summary}


def _summarize_text(name: str, title: str, text: str) -> tuple[str, list[str]]:
    """긴 원문(리포트 등) → 투자관점 요약 1~2문장 + 핵심 포인트. LLM 없으면 앞부분 발췌."""
    if llm.available():
        system = ("너는 한국 주식 애널리스트다. 아래 문서를 투자 관점에서 사실 기반으로 요약한다. "
                  "과장·추천 금지, 문서에 없는 내용 금지.")
        user = (f"종목: {name}\n제목: {title}\n본문:\n{text[:6000]}\n\n"
                'JSON으로만: {"summary": "한국어 1~2문장", "points": ["핵심 ≤3개"]}')
        out = llm.complete_json(system, user, max_tokens=500)
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


def import_file(ticker: str, name: str, filename: str, data: bytes, media_type: str) -> dict:
    """업로드 파일(PDF/이미지)을 KB 문서로. 네이티브 텍스트 PDF는 pypdf로 싸게, 스캔·이미지는
    vision(모델 OCR)으로 인식 → 요약·분류 후 적재. 반환: {ok, doc_class, summary, method}."""
    if not ticker or not name or not data:
        return {"ok": False, "reason": "ticker·name·파일 필요"}
    title = filename or f"{name} 업로드"
    text, method = "", ""
    if media_type == "application/pdf":
        text = _pdf_text(data)
    if len(text) >= _MIN_PDF_TEXT:
        summary, _ = _summarize_text(name, title, text)
        raw, method = text, "pdf_text"
    else:  # 스캔 PDF 또는 이미지 → 모델이 직접 인식(OCR)
        summary, _ = _summarize_vision(name, title, data, media_type)
        raw, method = "[스캔/이미지 문서 — 모델 인식]", "vision"
        if not summary:
            return {"ok": False, "reason": "문서 인식 실패(LLM 키 확인 또는 지원 형식인지 확인)"}
    # 검증: 텍스트 PDF는 원문으로, 스캔/이미지는 모델 요약문으로 신뢰성 심사
    v = validate_import(ticker, name, text if method == "pdf_text" else summary, title)
    if v["verdict"] == "reject":
        return {"ok": False, "verdict": "reject", "trust": v["trust"], "reasons": v["reasons"], "method": method,
                "reason": "KB 오염 우려로 저장하지 않음: " + (", ".join(v["reasons"]) or "신뢰도 낮음")}
    status = "confirmed" if v["verdict"] == "accept" else "pending"
    doc_class = classify_document({"title": title, "summary": summary}, "report")
    db.kb_document_add(ticker, title, summary, "", "upload", "", doc_class, raw_text=raw, status=status)
    if status == "confirmed":
        _rebuild_digest(ticker, name)
    return {"ok": True, "status": status, "verdict": v["verdict"], "trust": v["trust"],
            "reasons": v["reasons"], "doc_class": doc_class, "summary": summary, "method": method}


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
    """원자료 제목/요약에서 악재 이벤트 키워드를 찾아 (플래그, 사유) 반환. 없으면 (False, "")."""
    for it in items:
        text = f"{it.get('title', '')} {it.get('summary', '')}"
        for term in _EVENT_TERMS:
            if term in text:
                return True, f"{term} — {(it.get('title') or '').strip()[:60]}"
    return False, ""


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
    """원자료 → {sentiment[-1..1], summary(1문장), points[≤3]}. LLM 우선, 실패 시 규칙기반."""
    if not items:
        return {"sentiment": 0.0, "summary": "최근 수집된 뉴스·영상이 없습니다.", "points": []}
    if llm.available():
        headlines = "\n".join(f"- [{it.get('source', '')}] {it.get('title', '')} :: {it.get('summary', '')[:120]}"
                              for it in items[:12])
        system = ("너는 한국 주식 애널리스트다. 주어진 종목의 최근 뉴스·영상 헤드라인을 근거로 투자 관점의 "
                  "정성 요약을 만든다. 과장/추천 금지, 사실 기반. 헤드라인에 없는 내용은 지어내지 마라.")
        user = (f"종목: {name}\n최근 헤드라인:\n{headlines}\n\n"
                'JSON으로만: {"sentiment": -1.0~1.0 사이 실수(투자심리), '
                '"summary": "한국어 한 문장 요약", "points": ["핵심 포인트 최대 3개(한국어 짧게)"]}')
        out = llm.complete_json(system, user, max_tokens=500)
        if out and isinstance(out.get("sentiment"), (int, float)):
            s = max(-1.0, min(1.0, float(out["sentiment"])))
            pts = [str(p) for p in (out.get("points") or [])][:3]
            return {"sentiment": round(s, 2), "summary": str(out.get("summary", ""))[:200], "points": pts}
        log.info("LLM 다이제스트 파싱 실패 — 규칙기반 폴백")
    return _rule_digest(name, items)


def import_macro(title: str, text: str, url: str = "", published: str = "") -> dict:
    """시황·거시 내러티브(단일 종목 특정 불가)를 거시 KB(_MARKET)에 적재한다.
    개별 종목 검증(종목명 언급)은 적용하지 않되, 너무 짧은 글은 배제. 저장 후 거시 다이제스트 갱신."""
    text = (text or "").strip()
    if len(text) < 40:
        return {"ok": False, "reason": "본문이 너무 짧아 시황 KB에 저장하지 않음"}
    db.kb_document_add(MACRO_TICKER, title or "시황", text[:300], url, "insight",
                       published, "시황", raw_text=text, status="confirmed")
    _rebuild_macro_digest()
    return {"ok": True, "status": "confirmed", "doc_class": "시황"}


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
        out = llm.complete_json(system, user, max_tokens=500)
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
            "count": dg.get("n_sources"), "newest_ts": dg.get("newest_ts"), "fresh": fresh}


def refresh(targets: list[dict], news_n: int = 8, lookback_days: int = 7) -> dict:
    """targets: [{ticker, name}]. 각 종목 증권 뉴스 수집(신선도·관련성 필터)→저장→다이제스트 갱신.
    유튜브는 화이트리스트 확보 전까지 보류. 갱신 건수 반환."""
    updated = 0
    for t in targets:
        ticker, name = t.get("ticker"), t.get("name", "")
        if not ticker or not name:
            continue
        items = news.collect(name, news_n=news_n, lookback_days=lookback_days)
        if not items:
            continue
        for it in items:  # 문서 유형 분류(뉴스/실적/공시/이벤트/시황)
            it["doc_class"] = classify_document(it, "news")
        db.kb_entry_add_many(ticker, items)
        digest = build_digest(name, items)
        event_flag, event_note = detect_event(items)
        db.kb_digest_set(ticker, name, digest["sentiment"], digest["summary"], digest["points"],
                         len(items), newest_ts=_newest_ts(items), event_flag=event_flag, event_note=event_note)
        updated += 1
    return {"updated": updated}


def sentiment_map() -> dict[str, dict]:
    """ticker -> {score, reasons, event_risk, event_note} — engine이 소비.
    event_risk는 '최근(EVENT_TTL_DAYS 이내) 악재 이벤트'만 True(오래된 악재는 해제)."""
    now = time.time()
    out = {}
    for ticker, dg in db.kb_digests_all().items():
        if ticker.startswith("_"):  # 거시·시황 등 가상 종목은 개별 시그널에 반영 안 함(격리)
            continue
        reasons = []
        if dg.get("summary"):
            reasons.append(f"[정성] {dg['summary']}")
        fresh = dg.get("newest_ts") is None or (now - dg["newest_ts"]) <= EVENT_TTL_DAYS * 86400
        out[ticker] = {
            "score": dg.get("sentiment", 0.0), "reasons": reasons,
            "event_risk": bool(dg.get("event_flag")) and fresh,
            "event_note": dg.get("event_note") or "",
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


def collect_fanding(limit: int = 15, force: bool = False) -> dict:
    """fanding.kr 미주은 최신 포스트를 훑어 KB로 적재.
    - 종목 특정 글 → 종목 KB(전문가 인사이트, 검증기 게이트).
    - 종목 불특정이라도 시황·거시·시장흐름 해설 → 거시 KB(_MARKET, 시장흐름 트래킹·봇 자문용).
    - 순수 운영·홍보 공지(멤버십·결제·만화책 등)만 폐기.
    증분 수집: 이미 적재된 URL은 본문 조회·LLM 요약 없이 건너뛴다(force=True면 전량 재수집).
    반환: {imported:[...], macro:[...], skipped:[...], errors:[...]}."""
    from signal_desk.ingest import fanding
    if not config.fanding_cookie():
        return {"ok": False, "reason": "FANDING_TT 미설정(.env) — 자동수집 건너뜀"}
    index = _fanding_ticker_index()
    seen = set() if force else db.kb_document_urls(source="insight")
    imported, macro, skipped, errors = [], [], [], []
    for post in fanding.post_list(limit=limit):
        title = post.get("title") or ""
        url = fanding.post_url(post.get("post_no"))
        if url in seen:
            skipped.append({"post_no": post.get("post_no"), "title": title, "why": "이미 수집됨"})
            continue
        if any(w in title for w in _FANDING_NOISE):
            skipped.append({"post_no": post.get("post_no"), "title": title, "why": "운영·홍보 공지(폐기)"})
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
                                  source_type="insight", url=detail["url"], published=pub)
            if res.get("ok"):
                imported.append({"ticker": tk, "name": ko, "title": detail["title"],
                                 "status": res["status"], "trust": res.get("trust"), "published": pub})
            else:
                skipped.append({"post_no": post.get("post_no"), "title": title, "why": res.get("reason", "미저장")})
        else:  # 종목 불특정 → 시황·거시 내러티브로 적재
            res = import_macro(detail["title"], detail["content"], url=detail["url"], published=pub)
            if res.get("ok"):
                macro.append({"title": detail["title"], "published": pub})
            else:
                skipped.append({"post_no": post.get("post_no"), "title": title, "why": res.get("reason", "미저장")})
    log.info("fanding 수집: 종목 %d · 시황 %d · 스킵 %d · 오류 %d",
             len(imported), len(macro), len(skipped), len(errors))
    return {"ok": True, "imported": imported, "macro": macro, "skipped": skipped, "errors": errors}
