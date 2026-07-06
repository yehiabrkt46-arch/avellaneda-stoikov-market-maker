# tests/test_engine_hooks.py
from mm_bot.config import StrategyConfig
from mm_bot.feed.book import OrderBook
from mm_bot.feed.messages import BookSnapshot, Trade
from mm_bot.paper.engine import PaperEngine, StrategyLane
from mm_bot.store.db import Store
from mm_bot.strategy.base import QuotePair, Strategy


class SpyStrategy(Strategy):
    def __init__(self):
        self.name = "spy"
        self.mids = []
        self.trades = []

    def observe_mid(self, mid, ts_ms):
        self.mids.append((mid, ts_ms))

    def observe_trade(self, price, ts_ms):
        self.trades.append((price, ts_ms))

    def quotes(self, mid, position_usd, now_ms):
        return QuotePair(bid=None, ask=None)


def snapshot(ts):
    return BookSnapshot(
        instrument="BTC-PERPETUAL", change_id=1000, timestamp_ms=ts,
        bids=[(60000.0, 5000.0)], asks=[(60000.5, 4200.0)],
    )


def trade_ev(ts, price):
    return Trade(
        instrument="BTC-PERPETUAL", trade_id="t1", trade_seq=1,
        timestamp_ms=ts, price=price, amount=10.0, direction="sell",
    )


async def test_lane_feeds_strategy_hooks(tmp_path):
    cfg = StrategyConfig(name="spy")
    store = Store(tmp_path / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    strat = SpyStrategy()
    lane = StrategyLane(strat, cfg, store, "s1", adverse_horizon_ms=5000)
    engine = PaperEngine(book=OrderBook(), lanes=[lane], store=store, session_id="s1")
    engine.apply_book_event(snapshot(ts=1_000_000))
    await engine.on_event(snapshot(ts=1_000_000))
    await engine.on_event(trade_ev(ts=1_000_100, price=59990.0))
    assert strat.mids == [(60000.25, 1_000_000)]
    assert strat.trades == [(59990.0, 1_000_100)]
    store.close()


def test_base_strategy_hooks_are_noops():
    s = Strategy()
    s.observe_mid(60000.0, 0)   # must not raise
    s.observe_trade(60000.0, 0)  # must not raise
