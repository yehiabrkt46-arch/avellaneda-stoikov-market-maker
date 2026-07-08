# tests/test_fixed_spread.py
from mm_bot.config import StrategyConfig
from mm_bot.strategy.base import QuotePair, round_to_tick
from mm_bot.strategy.fixed_spread import FixedSpreadStrategy


def test_round_to_tick():
    assert round_to_tick(60001.3, 0.5, down=True) == 60001.0
    assert round_to_tick(60001.3, 0.5, down=False) == 60001.5
    assert round_to_tick(60001.5, 0.5, down=True) == 60001.5
    assert round_to_tick(60001.5, 0.5, down=False) == 60001.5


def test_symmetric_quotes_around_mid():
    cfg = StrategyConfig(half_spread_usd=5.0, tick_size=0.5)
    strat = FixedSpreadStrategy(cfg)
    q = strat.quotes(mid=60000.25, position_usd=0.0, now_ms=0)
    assert isinstance(q, QuotePair)
    assert q.bid == 59995.0  # 59995.25 floored to tick
    assert q.ask == 60005.5  # 60005.25 ceiled to tick
    assert q.bid < 60000.25 < q.ask


def test_quotes_ignore_inventory_and_time():
    cfg = StrategyConfig(half_spread_usd=5.0, tick_size=0.5)
    strat = FixedSpreadStrategy(cfg)
    a = strat.quotes(mid=60000.0, position_usd=0.0, now_ms=0)
    b = strat.quotes(mid=60000.0, position_usd=-5000.0, now_ms=999999)
    assert a == b


def test_strategy_exposes_name():
    cfg = StrategyConfig(name="baseline")
    assert FixedSpreadStrategy(cfg).name == "baseline"
