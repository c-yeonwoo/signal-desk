from collections import Counter

from signal_desk.reference import sectors


def test_no_duplicate_ticker_across_sectors():
    allt = [t for v in sectors._BY_SECTOR.values() for t in v]
    dupes = [t for t, c in Counter(allt).items() if c > 1]
    assert dupes == []


def test_granular_taxonomy_covers_korean_themes():
    names = set(sectors.sectors())
    for s in ["조선", "철강·금속", "화학", "통신", "항공", "엔터·미디어", "화장품", "로봇",
              "방산·우주항공", "은행·금융", "게임", "유통·리테일"]:
        assert s in names


def test_sector_of_known_names():
    assert sectors.sector_of("005930") == "반도체"
    assert sectors.sector_of("329180") == "조선"
    assert sectors.sector_of("090430") == "화장품"
    assert sectors.sector_of("454910") == "로봇"
    assert sectors.sector_of("999999") is None
