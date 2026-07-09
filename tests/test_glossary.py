"""용어 학습 사전 — 카테고리·항목 구조와 필수 필드."""

from signal_desk import api
from signal_desk.reference import glossary


def test_glossary_structure():
    cats = glossary.categories()
    assert len(cats) >= 5
    total = 0
    for c in cats:
        assert c["key"] and c["name"] and c["items"]
        for it in c["items"]:
            assert it["term"] and it["easy"] and it["why"] and it["in_signal"]
            total += 1
    assert total >= 20  # 충분한 학습 항목


def test_glossary_endpoint():
    assert api.glossary_get()["categories"] == glossary.categories()
