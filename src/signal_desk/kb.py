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

from signal_desk import db, llm
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
    """문서를 유형으로 분류. source_type이 명시되면(report/disclosure) 우선. 아니면 키워드 규칙."""
    if source_type == "report":
        return "리포트"
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    for cls, terms in _CLASS_RULES:
        terms = _EVENT_TERMS if cls == "이벤트" else terms
        if any(term.lower() in text for term in terms):
            return cls
    return "뉴스"


def import_document(ticker: str, name: str, title: str, text: str,
                    source_type: str = "report", url: str = "") -> dict:
    """증권사 리포트·원문 텍스트를 받아 LLM 요약·분류 후 KB 문서로 추가하고 다이제스트 갱신.
    반환: {ok, doc_class, summary}. 원문(raw_text)도 보존."""
    text = (text or "").strip()
    if not text or not ticker or not name:
        return {"ok": False, "reason": "ticker·name·text 필요"}
    doc_class = classify_document({"title": title, "summary": text[:500]}, source_type)
    summary, points = _summarize_text(name, title, text)
    db.kb_document_add(ticker, title or f"{name} {source_type}", summary, url,
                       source_type, "", doc_class, raw_text=text)
    _rebuild_digest(ticker, name)  # 뉴스+리포트 통합 재요약
    return {"ok": True, "doc_class": doc_class, "summary": summary}


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
    doc_class = classify_document({"title": title, "summary": summary}, "report")
    db.kb_document_add(ticker, title, summary, "", "upload", "", doc_class, raw_text=raw)
    _rebuild_digest(ticker, name)
    return {"ok": True, "doc_class": doc_class, "summary": summary, "method": method}


def _rebuild_digest(ticker: str, name: str) -> None:
    """해당 종목의 최근 KB 문서(뉴스+리포트)를 합쳐 다이제스트·이벤트·신선도를 재계산."""
    items = db.kb_entries_recent(ticker, 15)
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
