"""ML·검색 인프라·작업 큐 확대의 근거 현황. 비용을 늘리는 설치는 하지 않는다."""

from __future__ import annotations

VERSION = "research-scaling-readiness-v1"
MIN_SEARCH_SAMPLES = 100
SEARCH_P95_BUDGET_MS = 300
MIN_VECTOR_CORPUS = 10_000
MIN_LABELED_QUERIES = 100
MIN_ML_OUTCOMES_PER_MARKET = 100


def assess(*, document_count: int, latency: dict, prospective_cohorts: dict[str, int],
           review_pending: int) -> dict:
    samples = int(latency.get("samples") or 0)
    p95 = latency.get("p95_ms")
    doc_count = max(0, int(document_count))
    # 질의 라벨셋·작업 재시도/지연 원장은 아직 영속화되지 않았다. 모르는 값을 0으로
    # 꾸며서 '성능이 좋다'거나 '문제가 없다'고 해석하지 않는다.
    return {
        "version": VERSION, "mode": "research_only", "live_eligible": False,
        "metrics": {"confirmed_documents": doc_count, "search_latency_samples_30d": samples,
                    "search_p95_ms_30d": p95,
                    "prospective_cohorts": prospective_cohorts,
                    "review_pending": max(0, int(review_pending)),
                    "labeled_search_queries": None, "labeled_stock_outcomes": None,
                    "producer_retry_lag_p95_ms": None},
        "ml": {"status": "defer", "reason":
               f"시장별 최소 {MIN_ML_OUTCOMES_PER_MARKET}개 시점 검증된 개별 종목 정답·비용 후 기준선이 필요합니다. 현재 라벨 원장이 없습니다."},
        "vector_database": {"status": "defer", "reason":
                            (f"운영 검색 지연 표본이 {samples}/{MIN_SEARCH_SAMPLES}개입니다."
                             if samples < MIN_SEARCH_SAMPLES else
                             f"검색 p95 {round(float(p95 or 0), 1)}ms; 문서 {doc_count}개."
                             f" 코퍼스 {MIN_VECTOR_CORPUS}개 이상·p95 {SEARCH_P95_BUDGET_MS}ms 초과·"
                             f"대표 질의 {MIN_LABELED_QUERIES}개에서 품질 향상이 함께 확인돼야 합니다.")},
        "distributed_queue": {"status": "defer", "reason":
                              "사람 검토 대기 건수는 생산자 작업 큐의 실패·재시도 지연이 아닙니다. "
                              "작업별 지연·중복·재시도 측정과 단일 DB 병목 증거가 필요합니다."},
        "note": "관측치가 부족해 증설을 보류합니다. 측정 결과가 기준을 넘더라도 검색 품질·비용·운영 복잡도 비교 후 별도 승인합니다.",
    }
