"""ETF 구성종목 큐레이션 — 인사이트 탭 서클차트용 정적 참고 자료(시그널·KB 무관)."""

from signal_desk.reference import etfs


def test_all_etfs_shape():
    all_ = etfs.all_etfs()
    assert 10 <= len(all_) <= 15                 # '유명 10~15개'
    keys = {e["key"] for e in all_}
    assert len(keys) == len(all_)                # 키 중복 없음
    for e in all_:
        assert e["market"] in ("kr", "us")
        assert e["name"] and e["desc"]
        assert e["holdings"], f"{e['key']} 구성종목 없음"


def test_holdings_weights_valid_and_underfilled():
    for e in etfs.all_etfs():
        total = 0.0
        for h in e["holdings"]:
            assert h["name"]
            assert 0 < h["weight"] <= 100
            total += h["weight"]
        # 상위 일부만 담으므로 합은 100 미만 → 프런트가 '기타'로 채운다
        assert total <= 100, f"{e['key']} 비중 합 {total} > 100"


def test_both_markets_present():
    markets = {e["market"] for e in etfs.all_etfs()}
    assert markets == {"kr", "us"}
