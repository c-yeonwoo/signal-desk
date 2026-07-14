"""KB 문서 임베딩 — 하이브리드 검색·시맨틱 veto용.

백엔드(우선순위):
  1) openai — OPENAI_API_KEY 있으면 text-embedding-3-small
  2) local  — sentence-transformers 설치 시(optional extra `embed`)
  3) hashing — 항상 동작(토큰·한글2그램 피처 해싱). 파이프라인/테스트용 · 진짜 동의어 매칭은 약함

점수 팩터로 쓰지 않는다. 검색·악재 후보 탐지에만 사용.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import struct
import urllib.request
from functools import lru_cache

from signal_desk import db

log = logging.getLogger("signal_desk.kb_embed")

DIM = 384
MODEL_HASH = "hashing-v1"
MODEL_OPENAI = "text-embedding-3-small"
MODEL_LOCAL = "intfloat/multilingual-e5-small"

_WORD = re.compile(r"[a-z0-9]+", re.I)
_HANGUL = re.compile(r"[가-힣]+")

# 하이브리드·veto 임계(설정 한곳)
HYBRID_ALPHA = 0.55          # dense 가중(나머진 BM25). 임베딩 없으면 0으로 취급
EVENT_SEMANTIC_TAU = 0.78    # 보수적 — 오탐보다 미탐 허용(키워드 OR로 보완)
_OPENAI_URL = "https://api.openai.com/v1/embeddings"


def backend() -> str:
    """현재 활성 임베딩 백엔드 이름."""
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    try:
        import sentence_transformers  # noqa: F401
        return "local"
    except Exception:
        return "hashing"


def model_id() -> str:
    b = backend()
    if b == "openai":
        return MODEL_OPENAI
    if b == "local":
        return MODEL_LOCAL
    return MODEL_HASH


def semantic_capable() -> bool:
    """동의어·패러프레이즈에 의미 있는 dense인가(해시 폴백은 False)."""
    return backend() in ("openai", "local")


def _tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    toks = _WORD.findall(text)
    for seg in _HANGUL.findall(text):
        if len(seg) == 1:
            toks.append(seg)
        else:
            toks.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return toks


def _l2(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _hash_embed(text: str) -> list[float]:
    """의존성 0 폴백 — 검색 파이프라인·테스트용. 동의어 매칭력은 약함."""
    v = [0.0] * DIM
    for tok in _tokenize(text):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % DIM
        sign = 1.0 if (h >> 8) & 1 else -1.0
        v[idx] += sign
    return _l2(v)


def _openai_embed(texts: list[str]) -> list[list[float]] | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key or not texts:
        return None
    body = json.dumps({"model": MODEL_OPENAI, "input": texts}).encode("utf-8")
    req = urllib.request.Request(_OPENAI_URL, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        by_i = {row["index"]: row["embedding"] for row in data.get("data", [])}
        out = []
        for i in range(len(texts)):
            emb = by_i.get(i)
            if not emb:
                return None
            arr = [float(x) for x in emb]
            if len(arr) > DIM:
                arr = arr[:DIM]
            elif len(arr) < DIM:
                arr = arr + [0.0] * (DIM - len(arr))
            out.append(_l2(arr))
        return out
    except Exception as e:
        log.warning("OpenAI embed 실패: %s", type(e).__name__)
        return None


@lru_cache(maxsize=1)
def _local_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL_LOCAL)


def _local_embed(texts: list[str]) -> list[list[float]] | None:
    try:
        model = _local_model()
        # e5: query/passage 접두 — 문서·질의 모두 passage로 통일(단순)
        vecs = model.encode([f"passage: {t}" for t in texts], normalize_embeddings=True)
        out = []
        for row in vecs:
            arr = [float(x) for x in row]
            if len(arr) >= DIM:
                out.append(_l2(arr[:DIM]))
            else:
                out.append(_l2(arr + [0.0] * (DIM - len(arr))))
        return out
    except Exception as e:
        log.warning("local embed 실패: %s", type(e).__name__)
        return None


def embed_texts(texts: list[str]) -> list[list[float]]:
    """텍스트 목록 → L2 정규화 벡터(DIM). 실패 시 해싱으로 폴백(건별)."""
    if not texts:
        return []
    b = backend()
    if b == "openai":
        got = _openai_embed(texts)
        if got and len(got) == len(texts):
            return got
    elif b == "local":
        got = _local_embed(texts)
        if got and len(got) == len(texts):
            return got
    return [_hash_embed(t) for t in texts]


def embed_query(text: str) -> list[float]:
    if backend() == "local":
        try:
            model = _local_model()
            vec = model.encode([f"query: {text}"], normalize_embeddings=True)[0]
            arr = [float(x) for x in vec]
            return _l2(arr[:DIM] if len(arr) >= DIM else arr + [0.0] * (DIM - len(arr)))
        except Exception:
            pass
    return embed_texts([text])[0]


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


def entry_text(title: str | None, summary: str | None) -> str:
    return f"{(title or '').strip()} {(summary or '').strip()}".strip()


def upsert_entry(entry_id: int, title: str, summary: str) -> None:
    text = entry_text(title, summary)
    if not text:
        return
    vec = embed_texts([text])[0]
    db.kb_embedding_upsert(entry_id, model_id(), pack(vec))


def embed_missing(limit: int = 80) -> int:
    """임베딩 없는(또는 모델이 다른) confirmed 엔트리 증분 임베드. 처리 건수."""
    mid = model_id()
    rows = db.kb_entries_missing_embed(mid, limit=limit)
    if not rows:
        return 0
    texts = [entry_text(r["title"], r["summary"]) for r in rows]
    vecs = embed_texts(texts)
    done = 0
    for r, text, v in zip(rows, texts, vecs):
        if not text:
            continue
        db.kb_embedding_upsert(r["id"], mid, pack(v))
        done += 1
    return done


def load_vectors(entry_ids: list[int] | None = None) -> dict[int, list[float]]:
    """entry_id → vector(현재 모델만)."""
    rows = db.kb_embeddings_for_model(model_id(), entry_ids)
    return {eid: unpack(blob) for eid, blob in rows.items()}
