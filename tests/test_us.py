from signal_desk.reference import gurus


_UNIVERSE = [
    {"ticker": "AAPL", "name": "Apple Inc.", "sector": "Information Technology"},
    {"ticker": "KO", "name": "Coca-Cola", "sector": "Consumer Staples"},
    {"ticker": "GOOGL", "name": "Alphabet Inc. (Class A)", "sector": "Communication Services"},
]


def test_name_index_and_exact_match():
    idx = gurus.build_name_index(_UNIVERSE)
    # 13F 발행사명(대문자·법인격 표기)이 정규화 후 매칭돼야
    assert gurus.match_ticker("APPLE INC", idx) == "AAPL"
    assert gurus.match_ticker("COCA COLA CO", idx) == "KO"


def test_prefix_match_for_class_shares():
    idx = gurus.build_name_index(_UNIVERSE)
    assert gurus.match_ticker("ALPHABET INC", idx) == "GOOGL"  # 부분(접두) 매칭


def test_no_match_returns_none():
    idx = gurus.build_name_index(_UNIVERSE)
    assert gurus.match_ticker("SPDR S&P 500 ETF TRUST", idx) is None
    assert gurus.match_ticker("", idx) is None


def test_norm_strips_suffixes_and_punct():
    assert gurus._norm("Berkshire Hathaway Inc.") == "BERKSHIRE HATHAWAY"
    assert gurus._norm("AT&T CORP") == "AT T"
