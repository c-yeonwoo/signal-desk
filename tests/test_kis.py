import json
import time

import pytest

from signal_desk.broker import kis


def _creds(**overrides):
    base = {"app_key": "k", "app_secret": "s", "account_no": "50195913", "product_cd": "01", "env": "demo"}
    base.update(overrides)
    return base


def test_get_token_no_creds(monkeypatch):
    monkeypatch.setattr("signal_desk.config.kis_credentials", lambda: None)
    assert kis.get_token() is None


def test_get_token_uses_cache_without_network_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kis._TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    kis._TOKEN_FILE.write_text(json.dumps({"token": "cached-tok", "expires_at": time.time() + 3600}))

    def _boom(*a, **k):
        raise AssertionError("네트워크 호출이 발생하면 안 됨(캐시된 토큰을 써야 함)")
    monkeypatch.setattr("urllib.request.urlopen", _boom)

    assert kis.get_token(_creds()) == "cached-tok"


def test_get_token_ignores_expired_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kis._TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    kis._TOKEN_FILE.write_text(json.dumps({"token": "old-tok", "expires_at": time.time() - 10}))
    assert kis._load_cached_token() is None


def test_balance_parses_response(monkeypatch):
    fake_body = {
        "rt_cd": "0",
        "output1": [
            {"pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "10", "pchs_avg_pric": "70000",
             "prpr": "75000", "evlu_pfls_rt": "7.14"},
            {"pdno": "000660", "prdt_name": "SK하이닉스", "hldg_qty": "0", "pchs_avg_pric": "0"},
        ],
        "output2": [{"dnca_tot_amt": "5000000", "nass_amt": "5750000", "tot_evlu_amt": "5750000",
                     "evlu_amt_smtl_amt": "750000", "pchs_amt_smtl_amt": "700000",
                     "evlu_pfls_smtl_amt": "50000"}],
    }
    monkeypatch.setattr(kis, "_request", lambda *a, **k: fake_body)
    out = kis.balance(_creds())
    assert out["cash"] == 5_000_000.0           # 순자산 5,750,000 − 유가증권평가 750,000
    assert out["total_eval"] == 5_750_000.0     # 순자산
    assert out["stock_eval"] == 750_000.0
    assert out["pnl_pct"] == round(50000 / 700000 * 100, 2)  # 7.14%
    assert out["holdings"] == [{"ticker": "005930", "name": "삼성전자", "qty": 10, "avg_price": 70000.0,
                                "price": 75000.0, "pnl_pct": 7.14}]


def test_balance_returns_none_on_error(monkeypatch):
    monkeypatch.setattr(kis, "_request", lambda *a, **k: {"rt_cd": "1", "msg1": "실패"})
    assert kis.balance(_creds()) is None


def test_place_order_rejects_invalid_side():
    with pytest.raises(ValueError):
        kis.place_order("005930", "hold", 1, creds=_creds())


def test_place_order_market_vs_limit(monkeypatch):
    captured = {}

    def fake_request(path, tr_id, creds, params, method="GET"):
        captured["tr_id"] = tr_id
        captured["params"] = params
        return {"rt_cd": "0", "output": {"ODNO": "123", "ORD_TMD": "101010"}}

    monkeypatch.setattr(kis, "_request", fake_request)

    out = kis.place_order("005930", "buy", 3, creds=_creds())
    assert out == {"order_no": "123", "order_time": "101010"}
    assert captured["tr_id"] == "VTTC0012U"
    assert captured["params"]["ORD_DVSN"] == "01"  # 시장가(가격 미지정)
    assert captured["params"]["ORD_UNPR"] == "0"

    kis.place_order("005930", "sell", 2, price=71000, creds=_creds())
    assert captured["tr_id"] == "VTTC0011U"
    assert captured["params"]["ORD_DVSN"] == "00"  # 지정가
    assert captured["params"]["ORD_UNPR"] == "71000"
    assert captured["params"]["SLL_TYPE"] == "01"


def test_place_order_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(kis, "_request", lambda *a, **k: None)
    assert kis.place_order("005930", "buy", 1, creds=_creds()) is None
