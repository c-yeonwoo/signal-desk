from signal_desk.broker import execution


def test_kr_fill_applies_adverse_slippage_commission_and_sell_tax(monkeypatch):
    monkeypatch.delenv("PAPER_KR_SLIPPAGE_BPS", raising=False)
    monkeypatch.delenv("PAPER_KR_COMMISSION_BPS", raising=False)
    monkeypatch.delenv("PAPER_KR_SELL_TAX_BPS", raising=False)
    buy = execution.calculate(100_000, 1, "buy", "kr")
    sell = execution.calculate(100_000, 1, "sell", "kr")

    assert (buy.fill_price, buy.commission, buy.sell_tax, buy.cash_change) == (100050.0, 15.0075, 0.0, -100065.0075)
    assert (sell.fill_price, sell.commission, sell.sell_tax, sell.cash_change) == (99950.0, 14.9925, 199.9, 99735.1075)


def test_us_sell_adds_current_regulatory_fees_and_is_configurable(monkeypatch):
    monkeypatch.setenv("PAPER_US_SLIPPAGE_BPS", "0")
    sell = execution.calculate(100, 1000, "sell", "us")
    # SEC 0.206bp of $100k + FINRA $0.000195/share.
    assert sell.regulatory_fee == 2.255
    assert sell.cash_change == 99997.745

    monkeypatch.setenv("PAPER_US_SEC_SELL_BPS", "0")
    monkeypatch.setenv("PAPER_US_FINRA_TAF_PER_SHARE", "0")
    no_reg = execution.calculate(100, 1000, "sell", "us")
    assert no_reg.regulatory_fee == 0.0
