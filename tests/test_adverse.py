# tests/test_adverse.py
from mm_bot.paper.adverse import AdverseSelectionTracker


def make_tracker(horizon_ms=5000):
    results = []
    t = AdverseSelectionTracker(horizon_ms, lambda ref, move: results.append((ref, move)))
    return t, results


def test_buy_fill_adverse_when_mid_drops():
    t, results = make_tracker()
    t.add_fill(ref=1, side="buy", mid_at_fill=60000.0, ts_ms=1_000_000)
    t.on_mid(59990.0, 1_004_999)  # before horizon: nothing
    assert results == []
    t.on_mid(59990.0, 1_005_000)  # at horizon: resolves
    assert results == [(1, 10.0)]  # bought, mid dropped 10 = adverse +10


def test_sell_fill_adverse_when_mid_rises():
    t, results = make_tracker()
    t.add_fill(ref=2, side="sell", mid_at_fill=60000.0, ts_ms=1_000_000)
    t.on_mid(60007.0, 1_005_000)
    assert results == [(2, 7.0)]


def test_favorable_moves_are_negative():
    t, results = make_tracker()
    t.add_fill(ref=3, side="buy", mid_at_fill=60000.0, ts_ms=1_000_000)
    t.on_mid(60004.0, 1_006_000)
    assert results == [(3, -4.0)]


def test_multiple_fills_resolve_in_order():
    t, results = make_tracker()
    t.add_fill(ref=1, side="buy", mid_at_fill=60000.0, ts_ms=1_000_000)
    t.add_fill(ref=2, side="buy", mid_at_fill=60010.0, ts_ms=1_002_000)
    t.on_mid(60000.0, 1_005_500)  # resolves ref 1 only
    assert [r[0] for r in results] == [1]
    t.on_mid(60000.0, 1_007_000)  # resolves ref 2
    assert [r[0] for r in results] == [1, 2]


def test_uses_first_mid_at_or_after_horizon():
    t, results = make_tracker()
    t.add_fill(ref=1, side="buy", mid_at_fill=60000.0, ts_ms=1_000_000)
    t.on_mid(59980.0, 1_009_000)  # first observation past horizon wins
    t.on_mid(59900.0, 1_010_000)
    assert results == [(1, 20.0)]
