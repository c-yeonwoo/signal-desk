"""KB 문서 검색(RAG 검색기) — 챗봇이 "왜?"에 답할 때 관련 KB 원문 문서를 찾아준다.

하이브리드: BM25(한글 2-그램) + dense(kb_embed). dense 없으면 BM25만.
외부 벤더·임베딩 키가 없어도 BM25로 동작(그레이스풀).

의존성 0(표준 라이브러리만) 경로 유지. 코퍼스는 kb_entries 시그니처가 바뀔 때만 재색인.
"""

from __future__ import annotations

import math
import re

from signal_desk import db

_WORD = re.compile(r"[a-z0-9]+")
_HANGUL = re.compile(r"[가-힣]+")
_K1, _B = 1.5, 0.75


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
                       "summary": d.get("summary"), "url": d.get("url"), "doc_class": d.get("doc_class")})
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


def _minmax(pairs: list[tuple[float, int]]) -> dict[int, float]:
    if not pairs:
        return {}
    vals = [s for s, _ in pairs]
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return {i: 1.0 for _, i in pairs}
    return {i: (s - lo) / (hi - lo) for s, i in pairs}


def retrieve(query: str, k: int = 5, *, alpha: float | None = None) -> list[dict]:
    """질의와 관련 높은 KB 문서 top-k.
    반환: [{id,ticker,title,summary,url,doc_class,score,bm25,dense}] (hybrid 점수>0).
    alpha: dense 비중(기본 kb_embed.HYBRID_ALPHA). dense 후보 없으면 BM25만.
    """
    _ensure()
    corpus = _idx.get("corpus") or []
    if not corpus or not (query or "").strip():
        return []

    bm = _bm25_scores(query)
    dens = _dense_scores(query)
    bm_n = _minmax(bm)
    dens_n = _minmax(dens)

    try:
        from signal_desk import kb_embed
        a = kb_embed.HYBRID_ALPHA if alpha is None else float(alpha)
    except Exception:
        a = 0.0 if alpha is None else float(alpha)
    if not dens_n:
        a = 0.0  # dense 없으면 순수 BM25

    idxs = set(bm_n) | set(dens_n)
    scored = []
    for i in idxs:
        s = (1 - a) * bm_n.get(i, 0.0) + a * dens_n.get(i, 0.0)
        if s > 0:
            scored.append((s, i, bm_n.get(i, 0.0), dens_n.get(i, 0.0)))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for s, i, b, d in scored[:k]:
        doc = dict(corpus[i])
        doc["score"] = round(s, 3)
        doc["bm25"] = round(b, 3)
        doc["dense"] = round(d, 3)
        out.append(doc)
    return out
