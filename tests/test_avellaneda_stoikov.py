import pytest

from mm_bot.config import StrategyConfig
from mm_bot.paper.replay import build_strategy
from mm_bot.strategy.avellaneda_stoikov import AvellanedaStoikovStrategy


def make_strat(**over):
    params = dict(
        kind="avellaneda_stoikov", name="as", half_spread_usd=5.0,
        quote_size_usd=100.0, tick_size=0.5,
        gamma=0.01, horizon_s=60.0,
        vol_lambda=0.5, vol_min_dt_s=1.0, vol_min_samples=3,
        k_window_s=3600.0, k_min_trades=3,
    )
    params.update(over)
    return AvellanedaStoikovStrategy(StrategyConfig(**params))


def warm_up(strat, mid=60000.0):
    ts = 1_000_000
    strat.observe_mid(mid, ts)
    for i in range(1, 6):  # +2 USD per 1s -> sigma2 = 4
        strat.observe_mid(mid + 2.0 * i, ts + 1000 * i)
    last_mid = mid + 10.0  # after warmup loop the last observed mid is 60010
    for d in (20.0, 20.0, 20.0):  # mean distance 20 -> k = 0.05
        strat.observe_trade(last_mid + d, ts + 5000)
    return ts + 6000


def test_falls_back_to_fixed_spread_until_warm():
    strat = make_strat()
    q = strat.quotes(mid=60000.25, position_usd=0.0, now_ms=0)
    assert q.bid == 59995.0  # same as FixedSpread with half_spread 5
    assert q.ask == 60005.5


def test_warm_quotes_match_hand_computed_reference():
    strat = make_strat()
    now = warm_up(strat)
    # sigma2=4, tau=60, gamma=0.01, k=0.05, mid=60000, q=+2 (200 USD / 100 USD clip)
    # r = 60000 - 2*0.01*4*60 = 59995.2
    # delta = 1.2 + 100*ln(1.2) = 19.4321557
    q = strat.quotes(mid=60000.0, position_usd=200.0, now_ms=now)
    assert q.bid == 59975.5   # 59975.7678 floored to 0.5 tick
    assert q.ask == 60015.0   # 60014.6322 ceiled


def test_zero_inventory_quotes_are_symmetric_around_mid():
    strat = make_strat()
    now = warm_up(strat)
    q = strat.quotes(mid=60000.0, position_usd=0.0, now_ms=now)
    assert q.bid == pytest.approx(60000.0 - 19.5, abs=0.51)
    assert q.ask == pytest.approx(60000.0 + 19.5, abs=0.51)


def test_long_inventory_skews_quotes_down():
    strat = make_strat()
    now = warm_up(strat)
    flat = strat.quotes(mid=60000.0, position_usd=0.0, now_ms=now)
    long = strat.quotes(mid=60000.0, position_usd=300.0, now_ms=now)
    assert long.bid < flat.bid   # less eager to buy more
    assert long.ask < flat.ask   # more eager to sell


def test_observe_trade_uses_distance_from_last_mid():
    strat = make_strat(k_min_trades=1)
    strat.observe_mid(60000.0, 1_000_000)
    strat.observe_trade(60030.0, 1_000_500)  # distance 30
    assert strat._intensity.k() == pytest.approx(1.0 / 30.0)


def test_build_strategy_factory_dispatches():
    cfg = StrategyConfig(kind="avellaneda_stoikov", name="as")
    assert isinstance(build_strategy(cfg), AvellanedaStoikovStrategy)
