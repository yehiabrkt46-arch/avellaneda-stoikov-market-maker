# tests/test_portfolio.py
import pytest

from mm_bot.paper.portfolio import Fill, Portfolio


def fill(side, price, amount_usd, ts=1751800000000, trade_id="t1"):
    return Fill(timestamp_ms=ts, side=side, price=price, amount_usd=amount_usd, trade_id=trade_id)


def test_flat_portfolio_has_zero_equity():
    p = Portfolio()
    assert p.position_usd == 0.0
    assert p.equity_btc(60000.0) == 0.0


def test_buy_then_mark_up_matches_deribit_formula():
    p = Portfolio()
    p.apply_fill(fill("buy", 50000.0, 100.0))
    # long 100 USD (10 contracts), entry 50000, mark 100000
    # pnl_btc = 100 * (1/50000 - 1/100000) = 0.001
    assert p.equity_btc(100000.0) == pytest.approx(0.001)
    assert p.equity_usd(100000.0) == pytest.approx(100.0)


def test_round_trip_realizes_pnl_and_flattens():
    p = Portfolio()
    p.apply_fill(fill("buy", 50000.0, 100.0))
    p.apply_fill(fill("sell", 60000.0, 100.0))
    assert p.position_usd == pytest.approx(0.0)
    expected = 100.0 * (1 / 50000.0 - 1 / 60000.0)
    # flat position: equity independent of mark
    assert p.equity_btc(10.0) == pytest.approx(expected)
    assert p.equity_btc(1e9) == pytest.approx(expected)


def test_short_profits_when_price_falls():
    p = Portfolio()
    p.apply_fill(fill("sell", 60000.0, 100.0))
    assert p.position_usd == pytest.approx(-100.0)
    assert p.equity_btc(50000.0) == pytest.approx(100.0 * (1 / 50000.0 - 1 / 60000.0))
    assert p.equity_btc(50000.0) > 0


def test_long_loses_when_price_falls():
    p = Portfolio()
    p.apply_fill(fill("buy", 60000.0, 100.0))
    assert p.equity_btc(50000.0) < 0


def test_fill_count_increments():
    p = Portfolio()
    p.apply_fill(fill("buy", 50000.0, 10.0))
    p.apply_fill(fill("sell", 50000.0, 10.0))
    assert p.fill_count == 2
