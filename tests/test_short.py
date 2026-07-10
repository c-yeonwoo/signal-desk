"""공매도 팩터 — ISIN 산출, 파싱, 컴포넌트(임계·부호), store 조인, engine 연동."""

from signal_desk.ingest import krx_short
from signal_desk.signals import short as short_mod
from signal_desk.signals.engine import evaluate


def test_isin_check_digit_known():
    # 실검증된 ISIN(삼성전자·SK하이닉스·카카오)
    assert krx_short._isin("005930") == "KR7005930003"
    assert krx_short._isin("000660") == "KR7000660001"
    assert krx_short._isin("035720") == "KR7035720002"


def test_short_volume_parse(monkeypatch):
    body = ('{"OutBlock_1":['
            '{"TRD_DD":"2026/07/03","CVSRTSELL_TRDVOL":"374,046"},'
            '{"TRD_DD":"2026/07/02","CVSRTSELL_TRDVOL":"1,200"}]}')

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return body.encode()
    monkeypatch.setattr(krx_short, "_session", lambda: type("O", (), {"open": lambda *a, **k: _Resp()})())
    out = krx_short.short_volume("005930", 20)
    assert out == {"2026-07-03": 374046.0, "2026-07-02": 1200.0}


def test_short_volume_empty_returns_none(monkeypatch):
    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"OutBlock_1":[]}'
    monkeypatch.setattr(krx_short, "_session", lambda: type("O", (), {"open": lambda *a, **k: _Resp()})())
    assert krx_short.short_volume("005930") is None


def test_component_neutral_below_min():
    norm, w, reasons, ratio, has = short_mod.component({"short_ratio": 0.04, "days": 20}, 0.15)
    assert has is False and w == 0.0 and norm == 0.0 and ratio == 0.04   # 낮은 비중 → 제외


def test_component_penalizes_high_short():
    norm, w, reasons, ratio, has = short_mod.component({"short_ratio": 0.22, "days": 20}, 0.15)
    assert has is True and w == 0.15 and norm < 0 and "공매도" in reasons[0]
    # 22%면 -1로 포화(baseline 0.06, scale 0.16 → (0.22-0.06)/0.16=1.0)
    assert norm == -1.0


def test_component_none():
    assert short_mod.component(None, 0.15) == (0.0, 0.0, [], None, False)
    assert short_mod.component({}, 0.15)[4] is False


def test_component_monotonic():
    a = short_mod.component({"short_ratio": 0.10}, 0.15)[0]
    b = short_mod.component({"short_ratio": 0.15}, 0.15)[0]
    assert a > b                        # 비중 높을수록 더 큰 페널티(더 음수)


def test_store_fetch_short_join(tmp_path, monkeypatch):
    import importlib
    monkeypatch.chdir(tmp_path)
    from signal_desk import store
    importlib.reload(store)
    import pandas as pd
    (tmp_path / "data" / "cache").mkdir(parents=True)
    pd.DataFrame([
        {"date": "2026-07-02", "ticker": "005930", "open": 1, "close": 1, "volume": 1000.0},
        {"date": "2026-07-03", "ticker": "005930", "open": 1, "close": 1, "volume": 1000.0},
    ]).to_parquet(store.PRICES_FILE)
    from signal_desk.ingest import krx_short
    monkeypatch.setattr(krx_short, "short_volume",
                        lambda code, days=20: {"2026-07-02": 300.0, "2026-07-03": 100.0, "2026-06-30": 999.0})
    out = store.fetch_short([{"ticker": "005930"}], days=20)
    # 매칭된 2일만 사용: 400/2000 = 0.2
    assert out["005930"]["short_ratio"] == 0.2
    assert out["005930"]["days"] == 2 and out["005930"]["short_vol"] == 400


def test_evaluate_attaches_short_and_penalizes():
    closes = [100.0 + i * 0.1 for i in range(80)]
    uni = [{"ticker": "HI", "name": "고공매도"}, {"ticker": "LO", "name": "저공매도"}]
    shorts = {"HI": {"short_ratio": 0.25, "days": 20}, "LO": {"short_ratio": 0.03, "days": 20}}
    res = {r.ticker: r for r in evaluate(uni, {"HI": closes, "LO": closes}, shorts=shorts)}
    assert res["HI"].has_short is True and res["HI"].short_ratio == 0.25
    assert res["LO"].has_short is False           # 낮은 비중 → 제외
    assert res["HI"].score < res["LO"].score      # 공매도 페널티로 점수 하락
