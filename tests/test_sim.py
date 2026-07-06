# tests/test_sim.py
from mm_bot.feed.messages import Trade
from mm_bot.paper.portfolio import Fill
from mm_bot.paper.sim import FillSimulator


def trade(price, amount, ts=1751800000150, trade_id="t1", direction="sell"):
    return Trade(
        instrument="BTC-PERPETUAL",
        trade_id=trade_id,
        trade_seq=1,
        timestamp_ms=ts,
        price=price,
        amount=amount,
        direction=direction,
    )


def make_sim():
    fills = []
    sim = FillSimulator(fills.append)
    return sim, fills


def test_no_quotes_no_fills():
    sim, fills = make_sim()
    sim.on_trade(trade(59000.0, 100.0))
    assert fills == []


def test_trade_through_bid_fills_buy_at_quote_price():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, 60010.0, 100.0)
    sim.on_trade(trade(59999.5, 50.0))
    assert len(fills) == 1
    f = fills[0]
    assert f.side == "buy"
    assert f.price == 60000.0  # our quote price, not the trade print
    assert f.amount_usd == 50.0


def test_trade_at_bid_does_not_fill():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, 60010.0, 100.0)
    sim.on_trade(trade(60000.0, 50.0))  # at-touch, conservative rule: no fill
    assert fills == []


def test_trade_through_ask_fills_sell():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, 60010.0, 100.0)
    sim.on_trade(trade(60010.5, 30.0, direction="buy"))
    assert len(fills) == 1
    assert fills[0].side == "sell"
    assert fills[0].price == 60010.0
    assert fills[0].amount_usd == 30.0


def test_partial_fills_deplete_quote_and_conserve_quantity():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, None, 100.0)
    sim.on_trade(trade(59999.0, 60.0, trade_id="a"))
    sim.on_trade(trade(59998.0, 60.0, trade_id="b"))
    sim.on_trade(trade(59997.0, 60.0, trade_id="c"))  # quote exhausted, no fill
    assert [f.amount_usd for f in fills] == [60.0, 40.0]
    assert sum(f.amount_usd for f in fills) == 100.0  # never exceeds quote size
    assert fills[0].trade_id == "a" and fills[1].trade_id == "b"


def test_fill_never_exceeds_printed_trade_amount():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, None, 1000.0)
    sim.on_trade(trade(59999.0, 10.0))
    assert fills[0].amount_usd == 10.0


def test_set_quotes_replaces_and_resets_remaining():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, None, 100.0)
    sim.on_trade(trade(59999.0, 100.0))  # fully fill
    sim.on_trade(trade(59999.0, 100.0))  # nothing left
    assert len(fills) == 1
    sim.set_quotes(60000.0, None, 100.0)  # re-quote restores size
    sim.on_trade(trade(59999.0, 100.0))
    assert len(fills) == 2


def test_none_side_is_not_quoted():
    sim, fills = make_sim()
    sim.set_quotes(None, 60010.0, 100.0)
    sim.on_trade(trade(59000.0, 100.0))  # would cross a bid if one existed
    assert fills == []


def test_uses_exchange_timestamp():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, None, 100.0)
    sim.on_trade(trade(59999.0, 10.0, ts=1751800099999))
    assert fills[0].timestamp_ms == 1751800099999
