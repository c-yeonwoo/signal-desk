"""KB 문서 검색(RAG 검색기) — 챗봇이 "왜?"에 답할 때 관련 KB 원문 문서를 찾아준다.

문서 단위(kb_entries: 제목+요약)를 대상으로 BM25 랭킹. 한국어는 형태소 분석기(무거운 의존성)
대신 **한글 문자 2-그램 + 영숫자 토큰**으로 토크나이즈 — CJK에서 사전 없이도 잘 통하는 방식.
외부 벤더·임베딩 키가 없어도 동작(그레이스풀). 임베딩 벡터가 필요하면 이 모듈의 retrieve()만
임베딩 백엔드로 교체하면 챗봇 쪽은 그대로다(교체 지점 격리).

의존성 0(표준 라이브러리만). 코퍼스는 kb_entries 시그니처(건수·최대 id)가 바뀔 때만 재색인.
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
    corpus, tfs, dls = [], [], []
    df: dict[str, int] = {}
    for d in docs:
        toks = _tokenize((d.get("title") or "") + " " + (d.get("summary") or ""))
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t in tf:
            df[t] = df.get(t, 0) + 1
        corpus.append({"ticker": d.get("ticker"), "title": d.get("title"), "summary": d.get("summary"),
                       "url": d.get("url"), "doc_class": d.get("doc_class")})
        tfs.append(tf); dls.append(len(toks))
    n = len(corpus)
    idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
    _idx.update(sig=_signature(), corpus=corpus, tf=tfs, dl=dls,
                avgdl=(sum(dls) / n if n else 0.0), idf=idf)


def _ensure() -> None:
    if _idx.get("sig") != _signature():
        _build()


def retrieve(query: str, k: int = 5) -> list[dict]:
    """질의와 관련 높은 KB 문서 top-k. 반환: [{ticker,title,summary,url,doc_class,score}] (점수>0만)."""
    _ensure()
    corpus = _idx.get("corpus") or []
    if not corpus:
        return []
    q = set(_tokenize(query))
    idf, tf, dl, avgdl = _idx["idf"], _idx["tf"], _idx["dl"], _idx["avgdl"] or 1.0
    scored = []
    for i, doc in enumerate(corpus):
        s = 0.0
        for t in q:
            f = tf[i].get(t)
            if not f:
                continue
            s += idf.get(t, 0.0) * (f * (_K1 + 1)) / (f + _K1 * (1 - _B + _B * dl[i] / avgdl))
        if s > 0:
            scored.append((s, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{**d, "score": round(s, 3)} for s, d in scored[:k]]
