"""사람이 라벨링한 KB 질의로 BM25/의미 검색을 비용·품질 비교한다.

이 함수는 운영 검색 정책을 바꾸지 않는다. dense 평가에는 명시적 비용 허용이 필요하다.
합성 테스트는 동작 검사일 뿐 실제 검색 품질의 증거가 아니다.
"""

from __future__ import annotations

from signal_desk import kb_embed, kb_search


def evaluate_labeled(cases: list[dict], *, alpha: float = 0.0, k: int = 5,
                     allow_dense_cost: bool = False) -> dict:
    if not cases or not 1 <= k <= 20 or not 0 <= alpha <= 1:
        raise ValueError("라벨 질의와 1~20 범위 k, 0~1 범위 alpha가 필요합니다")
    if alpha > 0 and not allow_dense_cost:
        raise ValueError("의미 검색 평가는 임베딩 비용을 명시적으로 허용해야 합니다")
    recalls, rr, covered = [], [], 0
    for case in cases:
        query = str(case.get("query") or "").strip()
        relevant = {str(url) for url in case.get("relevant_urls") or [] if url}
        if not query or not relevant:
            raise ValueError("모든 질의에 검색어와 관련 문서 URL 라벨이 필요합니다")
        hits = kb_search.retrieve(query, k=k, alpha=alpha, recency=False)
        urls = [str(hit.get("url") or "") for hit in hits]
        recalls.append(len(relevant.intersection(urls)) / len(relevant))
        rr.append(next((1 / i for i, url in enumerate(urls, 1) if url in relevant), 0.0))
        covered += bool(hits)
    n = len(cases)
    return {"mode": "offline_search_evaluation", "live_eligible": False,
            "backend": kb_embed.model_id() if alpha > 0 else "bm25",
            "alpha": alpha, "queries": n, "k": k,
            "recall_at_k": round(sum(recalls) / n, 4),
            "mrr_at_k": round(sum(rr) / n, 4),
            "nonempty_rate": round(covered / n, 4),
            "note": "라벨셋의 대표성·실제 사용자 질의는 별도 검증 필요. 검색 성적은 투자 수익 성적이 아닙니다."}
