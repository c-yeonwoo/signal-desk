import pytest

from signal_desk import api
from signal_desk.signals.engine import SignalResult


def _sig(**kwargs):
    return SignalResult(name="Test", confidence=.7, technical_score=1.,
                        fundamental_score=0., has_fundamental=False, **kwargs)


@pytest.mark.parametrize("price_deferred", [False, True])
def test_us_response_summary_uses_its_row_generation_even_when_next_batch_differs(
        tmp_path, monkeypatch, price_deferred):
    monkeypatch.chdir(tmp_path)
    old = {"AAA":_sig(ticker="AAA",score=2.0,kind="BUY",rank=1,rank_eligible=True,
                      gate_blocked=False,low_coverage=False,data_coverage=1.,gates=[]),
           "BBB":_sig(ticker="BBB",score=.5,kind="HOLD",rank=2,rank_eligible=False,
                      gate_blocked=False,low_coverage=False,data_coverage=1.,gates=[])}
    calls = []

    def changing_batch():
        calls.append(True)
        return old if len(calls) == 1 else {
            "CCC":_sig(ticker="CCC",score=99.,kind="BUY",rank=1,rank_eligible=True,
                       gate_blocked=False,low_coverage=False,data_coverage=1.,gates=[])}

    monkeypatch.setattr(api,"_us_signals",changing_batch)
    monkeypatch.setattr(api.store,"load_us_universe",lambda:[])
    monkeypatch.setattr(api.store,"load_us_price_bundle",lambda:({"AAA":[100.,110.],"BBB":[50.,51.]},{}))
    monkeypatch.setattr(api.store,"us_marketcaps",lambda prices:{})
    monkeypatch.setattr(api.store,"usdkrw",lambda:None)
    monkeypatch.setattr(api.climate,"annotate_rows",lambda items:items)
    for name in ("_annotate_external_watch","_annotate_entry","_annotate_priced_in",
                 "_annotate_episode","_annotate_trader_layers"):
        monkeypatch.setattr(api,name,lambda items,**kw:items)
    monkeypatch.setattr(api,"_attach_lens_snapshot",lambda *a,**kw:{})

    def validity(items,*args):
        if price_deferred:
            items[0].update(kind="DEFERRED",rank_eligible=False,current_decision_status="deferred")

    monkeypatch.setattr(api,"_annotate_signal_price_status",validity)
    api._us_signal_items.cache_clear()
    try:
        body = api._signal_response("us")
        assert len(calls) == 1  # Old code read a different batch here.
        assert body["selection"]["universe"] == len(body["items"]) == 2
        assert body["selection"]["distribution"]["max"] == 2.
        assert body["selection"]["eligible"] == (0 if price_deferred else 1)
        assert [r["ticker"] for r in body["desk_report"]["buys"]] == ([] if price_deferred else ["AAA"])
        assert old["AAA"].kind == "BUY" and old["AAA"].rank_eligible
        # Item cache owns its original summary fields, not mutable engine objects.
        old["AAA"].score = 99.
        assert api._us_signal_items().results[0].score == 2.
    finally:
        api._us_signal_items.cache_clear()
