"""미국 시총이 원화 서식으로 렌더돼 대형주가 잡주로 보였다(2026-08-16).

`fmtMktcap` 은 조/억(원) 서식인데 미국 시총은 **달러**다. 그대로 씌운 결과 실측:

    USB   $101.3B (약 140조원)  → 화면 `1013억`
    EIX   $27.5B  (약 38조원)   → 화면 `275억`
    GL    $13.9B  (약 19조원)   → 화면 `139억`
    삼성전자 1,494조원            → 화면 `1494조`

나란히 놓이면 미국 대형주가 **1만배 작아 보인다**. "미국 쪽은 시총도 엄청 작은 잡주 같다"는
관찰의 원인이 유니버스가 아니라 이 서식이었다(유니버스는 S&P500뿐이고 최소가 $5.9B다).

같은 병이 세 곳 더 있었다 — 스크리너 `시총(억) ≥` 필터(달러 값을 억원과 비교해 `1000억
이상`이 미국에선 `$100B 이상`이 된다), 표의 시총순 정렬, 밸류체인 노드 선택(KR·US 혼합
정렬이라 미국이 늘 뒤로 밀린다).

규약: 환산은 **서버 한 곳**에서만 한다(`store.usdkrw`). 환율이 낡으면 None을 돌려 화면이
원 통화로 정직하게 그린다 — 틀린 환산보다 낫다.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest

from signal_desk import store

_HTML = Path(__file__).resolve().parents[1] / "src" / "signal_desk" / "web" / "index.html"


def _macro(value, asof):
    return [{"key": "CPIAUCSL", "value": 3.5, "asof": "2026-07-01"},
            {"key": "DEXKOUS", "label": "원/달러", "value": value, "asof": asof}]


def test_usdkrw_reads_the_rate(monkeypatch):
    monkeypatch.setattr(store, "load_macro", lambda: _macro(1409.94, str(datetime.date.today())))
    fx = store.usdkrw()
    assert fx and fx["rate"] == pytest.approx(1409.94) and fx["age_days"] == 0


def test_stale_rate_is_refused(monkeypatch):
    """낡은 환율로 환산하면 틀린 숫자가 조용히 화면에 남는다 — 없다고 말하는 편이 낫다."""
    old = datetime.date.today() - datetime.timedelta(days=store.USDKRW_MAX_AGE_DAYS + 1)
    monkeypatch.setattr(store, "load_macro", lambda: _macro(1409.94, old.isoformat()))
    assert store.usdkrw() is None


@pytest.mark.parametrize("macro", [
    [],                                                        # 수집 전
    [{"key": "DEXKOUS", "value": None, "asof": "2026-08-07"}],  # 값 없음
    [{"key": "DEXKOUS", "value": 0, "asof": "2026-08-07"}],     # 0으로 나누기
    [{"key": "DEXKOUS", "value": 1400, "asof": ""}],            # 시점 불명
])
def test_unusable_rate_is_none_not_a_guess(monkeypatch, macro):
    """**추측하지 않는다.** 0이나 기본값을 끼워 넣으면 전 종목 시총이 조용히 틀린다."""
    monkeypatch.setattr(store, "load_macro", lambda: macro)
    assert store.usdkrw() is None


def test_us_rows_carry_the_converted_marketcap(tmp_path, monkeypatch):
    """리스트가 환산값을 실어 보내야 화면이 국내와 **같은 축**으로 그린다."""
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.chdir(tmp_path)
    from signal_desk import db as db_module
    importlib.reload(db_module)
    from signal_desk import api
    importlib.reload(api)
    from signal_desk import store

    monkeypatch.setattr(store, "load_macro", lambda: _macro(1400.0, str(datetime.date.today())))
    monkeypatch.setattr(api, "_us_signal_items",
                        lambda: [{"ticker": "USB", "name": "U.S. Bancorp", "mktcap": 101_300_000_000,
                                  "kind": "HOLD", "score": 0.1},
                                 {"ticker": "NOCAP", "name": "없음", "mktcap": None,
                                  "kind": "HOLD", "score": 0.0}])
    monkeypatch.setattr(api, "_us_signals", lambda: {})
    for fn in ("_annotate_entry", "_annotate_priced_in", "_annotate_episode", "_annotate_trader_layers"):
        monkeypatch.setattr(api, fn, lambda items, market=None, **k: items)
    client = TestClient(api.app)
    client.post("/api/auth/signup", json={"email": "u@e.com", "pw": "abcdef12"})
    body = client.get("/api/signals?market=us").json()
    assert body.get("ready"), f"라우트가 막혔다: {body}"
    rows = {r["ticker"]: r for r in body["items"]}
    assert rows["USB"]["mktcap_krw"] == pytest.approx(101_300_000_000 * 1400.0)
    assert rows["NOCAP"].get("mktcap_krw") is None, "시총이 없으면 환산도 없다(0으로 채우지 않는다)"
    assert body["fx"]["rate"] == pytest.approx(1400.0), "화면이 환율 출처를 말할 수 있어야 한다"


def _js(name: str) -> str:
    src = _HTML.read_text(encoding="utf-8")
    i = src.index(f"function {name}(")
    return src[i:i + 700]


def test_every_marketcap_render_and_sort_goes_through_the_currency_aware_helper():
    """**한 곳이라도 빠지면 그 화면만 1만배 틀린다.** 원값을 직접 쓰는 자리가 없어야 한다.

    주석을 지운 뒤 센다 — 설명 주석에 `r.mktcap` 을 적으면 오탐이 난다(이 리포에서 세 번
    반복된 실수다).
    """
    src = _HTML.read_text(encoding="utf-8")
    body = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    # `fmtCap`/`capKrw` 는 통화를 판정한 **뒤에** 원값을 쓰는 게 맞다 — 헬퍼 본문은 뺀다.
    for name in ("capKrw", "fmtCap"):
        i = body.index(f"function {name}(")
        body = body[:i] + body[body.index("\n}", i) + 2:]
    assert "fmtMktcap(r.mktcap)" not in body, "표가 달러에 원화 서식을 씌운다"
    assert not re.search(r"[^\w][ab]\.mktcap\s*\|\|", body), \
        "시총을 원값으로 정렬한다 — KR(원)·US(달러)가 섞이면 미국이 늘 뒤로 밀린다"
    assert not re.search(r"\.mktcap\s*[-<>]\s*\w+\.mktcap", body), "원값끼리 비교한다"


def test_screener_cap_filter_does_not_silently_block_us():
    """억원 문턱을 달러 값과 비교하면 `1000억 이상`이 미국에선 `$100B 이상`이 된다."""
    src = re.sub(r"^\s*//.*$", "", _HTML.read_text(encoding="utf-8"), flags=re.M)
    line = next(l for l in src.splitlines() if "minCapEok" in l and "1e8" in l)
    assert "capKrw(" in line, f"시총 필터가 통화를 안 맞춘다: {line.strip()}"


def test_front_falls_back_to_dollars_when_the_rate_is_missing():
    """환율이 없을 때 원화 서식으로 그리면 틀린 숫자다 — `$`로 정직하게 그려야 한다."""
    fn = _js("fmtCap")
    assert "fmtUsd" in fn, "환율 없는 미국 시총을 달러로 그리는 경로가 없다"
    assert "'us'" in fn
