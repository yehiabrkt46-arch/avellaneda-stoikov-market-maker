# tests/test_engine.py
from mm_bot.config import StrategyConfig
from mm_bot.feed.book import OrderBook
from mm_bot.feed.messages import BookChange, BookSnapshot, Trade
from mm_bot.paper.engine import PaperEngine, StrategyLane
from mm_bot.paper.portfolio import Fill
from mm_bot.store.db import Store
from mm_bot.strategy.fixed_spread import FixedSpreadStrategy


def snapshot(ts, change_id=1000, bid=60000.0, ask=60000.5):
    return BookSnapshot(
        instrument="BTC-PERPETUAL", change_id=change_id, timestamp_ms=ts,
        bids=[(bid, 5000.0)], asks=[(ask, 4200.0)],
    )


def change(ts, change_id, prev, bid=60000.0, bids=None):
    return BookChange(
        instrument="BTC-PERPETUAL", change_id=change_id, prev_change_id=prev,
        timestamp_ms=ts,
        bids=bids if bids is not None else [("change", bid, 5000.0)],
        asks=[],
    )


def trade_ev(ts, price, amount=50.0, trade_id="t1"):
    return Trade(
        instrument="BTC-PERPETUAL", trade_id=trade_id, trade_seq=1,
        timestamp_ms=ts, price=price, amount=amount, direction="sell",
    )


def make_engine(tmp_path, half_spread=5.0):
    cfg = StrategyConfig(name="base", half_spread_usd=half_spread,
                         quote_size_usd=100.0, requote_interval_s=1.0)
    store = Store(tmp_path / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    book = OrderBook()
    lane = StrategyLane(FixedSpreadStrategy(cfg), cfg, store, "s1", adverse_horizon_ms=5000)
    engine = PaperEngine(book=book, lanes=[lane], store=store,
                         session_id="s1", rollup_interval_ms=60_000)
    return engine, book, lane, store


async def apply(engine, event):
    """Book events must hit the book before the engine, mirroring the live client."""
    engine.apply_book_event(event)
    await engine.on_event(event)


async def test_snapshot_triggers_first_quotes(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    # mid = 60000.25, half spread 5 -> bid 59995.0, ask 60005.5
    assert lane.sim.bid == (59995.0, 100.0)
    assert lane.sim.ask == (60005.5, 100.0)
    assert store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1


async def test_requote_respects_interval(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    await apply(engine, change(ts=1_000_500, change_id=1001, prev=1000))  # 0.5s: no requote
    assert store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
    await apply(engine, change(ts=1_001_000, change_id=1002, prev=1001))  # 1.0s: requote
    assert store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 2


async def test_trade_fill_updates_portfolio_and_persists(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    await engine.on_event(trade_ev(ts=1_000_100, price=59990.0, amount=50.0))
    assert lane.portfolio.position_usd == 50.0  # bid 59995 crossed
    row = store.connection.execute(
        "SELECT strategy, side, price, amount_usd, mid_at_fill FROM fills"
    ).fetchone()
    assert row == ("base", "buy", 59995.0, 50.0, 60000.25)


async def test_adverse_selection_resolved_and_stored(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    await engine.on_event(trade_ev(ts=1_000_100, price=59990.0))
    # mid moves down and 5s pass -> adverse resolves on next book event
    # real Deribit deltas move a level via delete old + new price
    await apply(engine, change(ts=1_006_000, change_id=1001, prev=1000,
                               bids=[("delete", 60000.0, 0.0), ("new", 59980.0, 5000.0)]))
    adverse = store.connection.execute(
        "SELECT adverse_move_usd FROM fills"
    ).fetchone()[0]
    # mid_at_fill 60000.25, mid now (59980+60000.5)/2 = 59990.25, buy: 60000.25-59990.25
    assert adverse == 10.0


async def test_rollup_written_on_interval(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    await apply(engine, change(ts=1_061_000, change_id=1001, prev=1000))
    assert store.connection.execute("SELECT COUNT(*) FROM rollups").fetchone()[0] == 1


async def test_trades_before_book_ready_are_ignored(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await engine.on_event(trade_ev(ts=1_000_000, price=59990.0))  # no book yet
    assert lane.portfolio.fill_count == 0


async def test_inventory_cap_suppresses_bid_via_lane(tmp_path):
    # default StrategyConfig.inventory_cap_usd is 500.0
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    mid = book.mid()
    lane.portfolio.position_usd = 500.0  # at the cap
    lane.portfolio.btc_cash = 500.0 / mid  # entered at ~mid, so equity is flat (no drawdown)
    await apply(engine, change(ts=1_001_000, change_id=1001, prev=1000))
    assert lane.sim.bid is None  # buying suppressed
    assert lane.sim.ask is not None  # unloading side stays quoted
    row = store.connection.execute("SELECT kind, detail FROM events").fetchone()
    assert row[0] == "cap_bind"
    assert "side=bid" in row[1]


async def test_kill_switch_stops_quoting_via_lane(tmp_path):
    # default StrategyConfig.max_drawdown_usd is 100.0
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000, bid=60000.0, ask=60000.5))
    lane._handle_fill(
        Fill(timestamp_ms=1_000_000, side="buy", price=60000.0, amount_usd=5000.0, trade_id="f1")
    )
    # mid drops far enough that the long position's mark-to-mid equity falls
    # more than max_drawdown_usd below the peak recorded on the first quote.
    await apply(engine, snapshot(ts=1_001_000, change_id=1001, bid=58790.0, ask=58790.5))
    assert lane.risk.killed
    assert lane.sim.bid is None
    assert lane.sim.ask is None
    row = store.connection.execute("SELECT kind FROM events").fetchone()
    assert row[0] == "kill_switch"
    # stays dead even after mid recovers
    await apply(engine, snapshot(ts=1_002_000, change_id=1002, bid=60000.0, ask=60000.5))
    assert lane.sim.bid is None
    assert lane.sim.ask is None
