"""KB 문서 검색(RAG 검색기) — 챗봇이 "왜?"에 답할 때 관련 KB 원문 문서를 찾아준다.

하이브리드: BM25(한글 2-그램) + dense(kb_embed). dense 없으면 BM25만.
외부 벤더·임베딩 키가 없어도 BM25로 동작(그레이스풀).

**시점이 관련도와 같은 급의 1급 신호다.** 오전에 사실이던 시황은 한 시간 뒤 사실이 아닐 수 있는데,
관련도만 보는 검색기는 3주 전 기사를 오늘 기사와 같은 순위에 올린다. 그래서 (a) 문서 나이를
유형별 반감기로 감쇠해 순위에 반영하고, (b) 나이·시점을 결과에 **항상** 실어 보낸다. 소비자가
시점을 모르면 신선도는 존재하지 않는 것과 같다.

의존성 0(표준 라이브러리만) 경로 유지. 코퍼스는 kb_entries 시그니처가 바뀔 때만 재색인.
"""

from __future__ import annotations

import datetime
import math
import re
import time

from signal_desk import db

_WORD = re.compile(r"[a-z0-9]+")
_HANGUL = re.compile(r"[가-힣]+")
_K1, _B = 1.5, 0.75

# 유형별 반감기(일) — 정보가 절반 쓸모없어지는 시간. 순위 감쇠에만 쓰고 판정에는 쓰지 않는다.
# 근거는 정보의 성질이다: 시황·뉴스는 하루만 지나도 전제가 바뀌고, 공시·실적은 사건 자체가
# 사실로 남고, 리포트의 밸류에이션 논리는 분기 단위로 유효하다.
HALF_LIFE_DAYS = {"시황": 2.0, "뉴스": 4.0, "이벤트": 7.0, "실적": 30.0, "공시": 30.0, "리포트": 45.0}
DEFAULT_HALF_LIFE_DAYS = 7.0
# 반감기의 이 배수를 넘으면 '오래됨'으로 표시(fresh_only=True면 후보에서 제외).
STALE_MULT = 3.0
RECENCY_FLOOR = 0.15   # 감쇠 하한 — 오래된 문서를 순위에서 지우지는 않는다(맥락은 남는다)


def _tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    toks = _WORD.findall(text)                      # 영문·숫자 토큰(티커·PER 등)
    for seg in _HANGUL.findall(text):               # 한글은 문자 2-그램(사전 없이 부분일치)
        toks.append(seg) if len(seg) == 1 else toks.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return toks


_idx: dict = {"sig": None}


def _signature() -> tuple:
    c = db.conn()
    try:
        row = c.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM kb_entries").fetchone()
    finally:
        c.close()
    return tuple(row)


def doc_ts(doc: dict) -> float | None:
    """문서의 기준 시각(epoch). published 우선, 파싱 실패 시 fetched. 둘 다 없으면 None."""
    from signal_desk.ingest import news
    dt = news._parse_dt((doc.get("published") or "").strip())
    if dt is not None:
        return dt.timestamp()
    fetched = doc.get("fetched")
    try:
        return float(fetched) if fetched else None
    except (TypeError, ValueError):
        return None


def age_days(doc: dict, *, now: float | None = None) -> float | None:
    """문서 나이(일). 시각을 모르면 None — 이때는 감쇠하지 않고 '시점 불명'으로 알린다."""
    ts = doc_ts(doc)
    if ts is None:
        return None
    return max(0.0, ((now or time.time()) - ts) / 86400.0)


def half_life(doc_class: str | None) -> float:
    return HALF_LIFE_DAYS.get((doc_class or "").strip(), DEFAULT_HALF_LIFE_DAYS)


def recency_weight(doc: dict, *, now: float | None = None) -> float:
    """0.15~1.0 감쇠 계수. 나이 불명은 1.0이 아니라 중간값을 준다 — 모르는 것을 최신으로
    대우하면 시점 없는 업로드 문서가 항상 상위를 먹는다."""
    a = age_days(doc, now=now)
    if a is None:
        return 0.5
    return max(RECENCY_FLOOR, 0.5 ** (a / half_life(doc.get("doc_class"))))


def _build() -> None:
    docs = db.kb_documents(limit=5000)
    corpus, tfs, dls, ids = [], [], [], []
    df: dict[str, int] = {}
    for d in docs:
        toks = _tokenize((d.get("title") or "") + " " + (d.get("summary") or ""))
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t in tf:
            df[t] = df.get(t, 0) + 1
        corpus.append({"id": d.get("id"), "ticker": d.get("ticker"), "title": d.get("title"),
                       "summary": d.get("summary"), "url": d.get("url"), "doc_class": d.get("doc_class"),
                       # 시점은 코퍼스에 함께 담는다 — 여기서 버리면 소비자가 나이를 알 길이 없다.
                       "published": d.get("published"), "fetched": d.get("fetched")})
        tfs.append(tf); dls.append(len(toks)); ids.append(d.get("id"))
    n = len(corpus)
    idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
    _idx.update(sig=_signature(), corpus=corpus, tf=tfs, dl=dls, ids=ids,
                avgdl=(sum(dls) / n if n else 0.0), idf=idf)


def _ensure() -> None:
    if _idx.get("sig") != _signature():
        _build()


def _bm25_scores(query: str) -> list[tuple[float, int]]:
    """(score, corpus_index) 점수>0만."""
    corpus = _idx.get("corpus") or []
    if not corpus:
        return []
    q = set(_tokenize(query))
    idf, tf, dl, avgdl = _idx["idf"], _idx["tf"], _idx["dl"], _idx["avgdl"] or 1.0
    scored = []
    for i, _doc in enumerate(corpus):
        s = 0.0
        for t in q:
            f = tf[i].get(t)
            if not f:
                continue
            s += idf.get(t, 0.0) * (f * (_K1 + 1)) / (f + _K1 * (1 - _B + _B * dl[i] / avgdl))
        if s > 0:
            scored.append((s, i))
    return scored


def _dense_scores(query: str) -> list[tuple[float, int]]:
    """cosine dense 점수(>0)와 corpus index. 임베딩/벡터 없으면 []."""
    try:
        from signal_desk import kb_embed
    except Exception:
        return []
    corpus = _idx.get("corpus") or []
    ids = [d.get("id") for d in corpus if d.get("id")]
    if not ids:
        return []
    # 검색 직전 소량 백필(신규 문서)
    try:
        kb_embed.embed_missing(limit=40)
    except Exception:
        pass
    vecs = kb_embed.load_vectors([i for i in ids if i is not None])
    if not vecs:
        return []
    qv = kb_embed.embed_query(query)
    out = []
    for i, doc in enumerate(corpus):
        eid = doc.get("id")
        dv = vecs.get(eid) if eid is not None else None
        if not dv:
            continue
        s = kb_embed.cosine(qv, dv)
        if s > 0.05:
            out.append((s, i))
    return out


_NORM_FLOOR = 0.05   # 최하위 후보를 0으로 만들면 '점수>0' 필터에 걸려 조용히 탈락한다


def _by_max(pairs: list[tuple[float, int]]) -> dict[int, float]:
    """BM25 정규화 — 최댓값으로 나눠 **비율을 보존**한다.

    min-max를 쓰면 안 되는 이유: 후보가 적을 때 사소한 차이가 후보 집합의 폭에 맞춰 최대로
    벌어진다. 제목 길이만 다른 사실상 동점 문서 2건이 1.0 대 0.0이 되고, 그 20배 격차가
    최신성 감쇠를 전부 삼킨다. 비율을 보존하면 동점은 동점으로 남아 최신성이 결정한다.
    """
    if not pairs:
        return {}
    hi = max(s for s, _ in pairs) or 1.0
    return {i: max(_NORM_FLOOR, s / hi) for s, i in pairs}


# dense 정규화 감도 — 이만큼 cosine이 벌어지면 순위가 확실히 갈린다. 실측(e5-small, 297건)에서
# 무관 질의 중위 0.790 · 관련 질의 중위 0.812로 유의미 밴드가 0.1 수준이었다.
DENSE_SPREAD = 0.10


def _dense_norm(pairs: list[tuple[float, int]]) -> dict[int, float]:
    """dense(cosine) 정규화 — 후보 평균에서 얼마나 벗어났는지를 **고정 감도**로 환산한다.

    min-max를 쓰면 안 되는 이유: cosine 차이가 0.002뿐인 사실상 동점 후보도 0.05 대 1.0으로
    벌어지고, 그 격차가 dense 가중(0.55)을 그대로 먹는다. 없는 신호를 최대로 증폭하는 셈이다.
    평균 대비 고정 감도면 진짜 벌어졌을 때만 순위가 갈리고, 동점일 때는 중립(0.5)에 머문다.
    """
    if not pairs:
        return {}
    vals = [s for s, _ in pairs]
    mean = sum(vals) / len(vals)
    return {i: min(1.0, max(_NORM_FLOOR, 0.5 + (s - mean) / DENSE_SPREAD)) for s, i in pairs}


def retrieve(query: str, k: int = 5, *, alpha: float | None = None,
             fresh_only: bool = False, now: float | None = None,
             recency: bool = True) -> list[dict]:
    """질의와 관련 높은 KB 문서 top-k.
    반환: [{id,ticker,title,summary,url,doc_class,score,relevance,bm25,dense,
           age_days,as_of,stale,half_life_days}] (hybrid 점수>0).
    alpha: dense 비중(기본 kb_embed.HYBRID_ALPHA). dense 후보 없으면 BM25만.
    fresh_only: 반감기 STALE_MULT배를 넘은 문서를 후보에서 제외(속보성 질의용).
    recency=False면 감쇠 없이 순수 관련도(회귀 비교·디버그용). 나이 필드는 그래도 채운다.
    """
    _ensure()
    corpus = _idx.get("corpus") or []
    if not corpus or not (query or "").strip():
        return []

    bm = _bm25_scores(query)
    dens = _dense_scores(query)
    bm_n = _by_max(bm)        # 비율 보존 — 동점을 동점으로 남긴다
    dens_n = _dense_norm(dens)   # 평균 대비 고정 감도 — 동점이면 중립

    try:
        from signal_desk import kb_embed
        a = kb_embed.HYBRID_ALPHA if alpha is None else float(alpha)
    except Exception:
        a = 0.0 if alpha is None else float(alpha)
    if not dens_n:
        a = 0.0  # dense 없으면 순수 BM25

    # 후보는 **어휘 신호(BM25)가 있는 문서**로 제한하고 dense는 그 안에서 재정렬만 한다.
    # 의미 벡터는 '아무것도 안 맞음'을 못 가른다 — 실측(2026-07-27, e5-small, 코퍼스 297건):
    # 무관 질의("고양이 사료")의 최고 cosine 0.844 vs 관련 질의 0.873, 중위는 각각 0.790·0.812로
    # 사실상 같다. dense가 후보를 만들면 어떤 질의에도 상위 k개가 나와서 검색이 늘 '찾았다'고 한다.
    idxs = set(bm_n) if dens_n else set(bm_n) | set(dens_n)
    t_now = now or time.time()
    scored = []
    for i in idxs:
        rel = (1 - a) * bm_n.get(i, 0.0) + a * dens_n.get(i, 0.0)
        if rel <= 0:
            continue
        doc = corpus[i]
        aged = age_days(doc, now=t_now)
        hl = half_life(doc.get("doc_class"))
        stale = aged is not None and aged > hl * STALE_MULT
        if fresh_only and stale:
            continue
        w = recency_weight(doc, now=t_now) if recency else 1.0
        scored.append((rel * w, rel, i, bm_n.get(i, 0.0), dens_n.get(i, 0.0), aged, hl, stale))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for s, rel, i, b, d, aged, hl, stale in scored[:k]:
        doc = dict(corpus[i])
        doc["score"] = round(s, 3)
        doc["relevance"] = round(rel, 3)   # 감쇠 전 관련도 — 왜 밀렸는지 보이게
        doc["bm25"] = round(b, 3)
        doc["dense"] = round(d, 3)
        doc["age_days"] = None if aged is None else round(aged, 1)
        doc["half_life_days"] = hl
        doc["stale"] = bool(stale)
        ts = doc_ts(doc)
        doc["as_of"] = (datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                        if ts else None)
        out.append(doc)
    return out
