# tests/test_risk.py
from mm_bot.config import StrategyConfig
from mm_bot.risk import RiskManager
from mm_bot.strategy.base import QuotePair


def make_risk(inventory_cap_usd=500.0, max_drawdown_usd=100.0):
    cfg = StrategyConfig(inventory_cap_usd=inventory_cap_usd, max_drawdown_usd=max_drawdown_usd)
    events = []
    risk = RiskManager(cfg, lambda kind, detail, ts_ms: events.append((kind, detail, ts_ms)))
    return risk, events


def test_cap_binds_long_side_suppresses_bid():
    risk, events = make_risk(inventory_cap_usd=500.0)
    q = risk.filter_quotes(
        QuotePair(bid=59995.0, ask=60005.0), position_usd=500.0, equity_usd=0.0, ts_ms=1000,
    )
    assert q == QuotePair(bid=None, ask=60005.0)
    assert events == [("cap_bind", "side=bid position_usd=500.00", 1000)]


def test_cap_binds_short_side_suppresses_ask():
    risk, events = make_risk(inventory_cap_usd=500.0)
    q = risk.filter_quotes(
        QuotePair(bid=59995.0, ask=60005.0), position_usd=-500.0, equity_usd=0.0, ts_ms=1000,
    )
    assert q == QuotePair(bid=59995.0, ask=None)
    assert events == [("cap_bind", "side=ask position_usd=-500.00", 1000)]


def test_cap_bind_emitted_once_per_episode():
    risk, events = make_risk(inventory_cap_usd=500.0)
    risk.filter_quotes(QuotePair(59995.0, 60005.0), 500.0, 0.0, 1000)
    risk.filter_quotes(QuotePair(59995.0, 60005.0), 600.0, 0.0, 2000)
    risk.filter_quotes(QuotePair(59995.0, 60005.0), 700.0, 0.0, 3000)
    assert [e[0] for e in events] == ["cap_bind"]


def test_cap_unbind_re_quotes_and_emits_once():
    risk, events = make_risk(inventory_cap_usd=500.0)
    risk.filter_quotes(QuotePair(59995.0, 60005.0), 500.0, 0.0, 1000)  # binds bid
    q = risk.filter_quotes(QuotePair(59995.0, 60005.0), 400.0, 0.0, 2000)  # unbinds
    assert q == QuotePair(bid=59995.0, ask=60005.0)  # both sides re-quoted
    q2 = risk.filter_quotes(QuotePair(59995.0, 60005.0), 300.0, 0.0, 3000)  # still unbound
    assert q2 == QuotePair(bid=59995.0, ask=60005.0)
    assert [e[0] for e in events] == ["cap_bind", "cap_unbind"]


def test_unloading_side_stays_quoted_while_capped():
    risk, _events = make_risk(inventory_cap_usd=500.0)
    q = risk.filter_quotes(QuotePair(59995.0, 60005.0), 500.0, 0.0, 1000)
    assert q.ask == 60005.0  # sell side (unloads the long) stays quoted


def test_kill_switch_trips_on_drawdown_from_peak_not_start():
    risk, events = make_risk(max_drawdown_usd=100.0)
    # start flat, equity rises to a new peak, then drops below the peak by
    # more than the limit while still above the very first observed equity.
    risk.filter_quotes(QuotePair(1.0, 2.0), 0.0, equity_usd=0.0, ts_ms=1000)
    risk.filter_quotes(QuotePair(1.0, 2.0), 100.0, equity_usd=500.0, ts_ms=2000)  # new peak
    q = risk.filter_quotes(QuotePair(1.0, 2.0), 100.0, equity_usd=380.0, ts_ms=3000)
    # drawdown from peak (500 - 380 = 120) exceeds 100 -> killed, even though
    # 380 > 0 (the starting equity), which a "from start" measure would miss.
    assert q == QuotePair(bid=None, ask=None)
    assert risk.killed
    assert events == [
        ("kill_switch", "peak_equity_usd=500.0000 equity_usd=380.0000 position_usd=100.00", 3000)
    ]


def test_kill_switch_does_not_trip_within_limit():
    risk, events = make_risk(max_drawdown_usd=100.0)
    risk.filter_quotes(QuotePair(1.0, 2.0), 0.0, equity_usd=500.0, ts_ms=1000)
    q = risk.filter_quotes(QuotePair(1.0, 2.0), 0.0, equity_usd=420.0, ts_ms=2000)
    assert q == QuotePair(1.0, 2.0)
    assert not risk.killed
    assert events == []


def test_killed_lane_never_quotes_again():
    risk, events = make_risk(max_drawdown_usd=100.0)
    risk.filter_quotes(QuotePair(1.0, 2.0), 0.0, equity_usd=500.0, ts_ms=1000)
    risk.filter_quotes(QuotePair(1.0, 2.0), 0.0, equity_usd=380.0, ts_ms=2000)  # kills
    assert risk.killed
    # even if equity recovers past the old peak, quoting stays dead forever
    q = risk.filter_quotes(QuotePair(1.0, 2.0), 0.0, equity_usd=10_000.0, ts_ms=3000)
    assert q == QuotePair(bid=None, ask=None)
    assert [e[0] for e in events] == ["kill_switch"]  # no duplicate events


def test_equity_none_skips_drawdown_check():
    risk, events = make_risk(max_drawdown_usd=100.0)
    q = risk.filter_quotes(QuotePair(1.0, 2.0), 0.0, equity_usd=None, ts_ms=1000)
    assert q == QuotePair(1.0, 2.0)
    assert not risk.killed
    assert events == []
