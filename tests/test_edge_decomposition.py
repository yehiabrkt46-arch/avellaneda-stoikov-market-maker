# tests/test_edge_decomposition.py
import pytest

from mm_bot.research.edge import (
    adverse_selection_usd,
    aggregate_fill_edge_by_day,
    day_bucket,
    spread_capture_usd,
)
from mm_bot.store.db import Store


def test_spread_capture_is_positive_for_a_favorable_buy():
    # buy 100 USD notional at 100, mid_at_fill is 100.5 above the fill price:
    # flattening at mid immediately locks in the spread.
    sc = spread_capture_usd("buy", price=100.0, amount_usd=100.0, mid_at_fill=100.5)
    assert sc == pytest.approx(0.5)


def test_adverse_selection_is_zero_when_mid_does_not_move():
    sc = spread_capture_usd("buy", price=100.0, amount_usd=100.0, mid_at_fill=100.5)
    asel = adverse_selection_usd(
        "buy", price=100.0, amount_usd=100.0, mid_at_fill=100.5, adverse_move_usd=0.0
    )
    assert asel == 0.0
    # the two components must sum exactly to the total P&L to the horizon,
    # which here (zero mid move) is just the spread capture.
    assert sc + asel == pytest.approx(sc)


def test_adverse_selection_is_negative_when_buy_gets_adversed_against():
    # adverse.py convention: for a buy, a positive adverse_move_usd means the
    # mid dropped after the fill (mid_forward = mid_at_fill - adverse_move_usd).
    # a buyer caught by a drop should show negative adverse selection P&L,
    # on top of whatever spread was captured.
    asel = adverse_selection_usd(
        "buy", price=100.0, amount_usd=100.0, mid_at_fill=100.5, adverse_move_usd=1.0
    )
    assert asel < 0.0
    assert asel == pytest.approx(-1.0)


def test_adverse_selection_is_positive_when_buy_gets_favorable_move():
    # mid rose after the fill (adverse_move_usd negative under the buy
    # convention): this is a bonus on top of spread capture, not adverse.
    asel = adverse_selection_usd(
        "buy", price=100.0, amount_usd=100.0, mid_at_fill=100.5, adverse_move_usd=-1.0
    )
    assert asel > 0.0
    assert asel == pytest.approx(1.0)


def test_adverse_selection_is_negative_when_sell_gets_adversed_against():
    # for a sell, a positive adverse_move_usd means the mid rose after the
    # fill (mid_forward = mid_at_fill + adverse_move_usd), against a seller.
    asel = adverse_selection_usd(
        "sell", price=100.5, amount_usd=100.0, mid_at_fill=100.0, adverse_move_usd=1.0
    )
    assert asel < 0.0


def test_components_sum_to_total_pnl_to_horizon():
    side, price, amount_usd, mid_at_fill, adverse_move_usd = "sell", 101.0, 50.0, 100.5, 1.5
    sc = spread_capture_usd(side, price, amount_usd, mid_at_fill)
    asel = adverse_selection_usd(side, price, amount_usd, mid_at_fill, adverse_move_usd)
    mid_forward = mid_at_fill + adverse_move_usd
    total = spread_capture_usd(side, price, amount_usd, mid_forward)
    # total P&L to the horizon, recomputed directly at mid_forward, must
    # equal the sum of the two decomposed components.
    assert sc + asel == pytest.approx(total)


def test_day_bucket_known_values():
    assert day_bucket(0) == 0
    assert day_bucket(86_400_000 - 1) == 0
    assert day_bucket(86_400_000) == 1
    assert day_bucket(172_800_000) == 2
    assert day_bucket(172_800_000 + 500) == 2


def test_aggregate_fill_edge_by_day_end_to_end(tmp_path):
    store = Store(tmp_path / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")

    # day 0, strategy fixed_spread: two fills, both with resolved adverse moves.
    fill1 = store.record_fill(
        "s1", 1000, "fixed_spread",
        side="buy", price=100.0, amount_usd=100.0, trade_id="a", mid_at_fill=100.5,
    )
    store.set_adverse(fill1, 0.0)
    fill2 = store.record_fill(
        "s1", 2000, "fixed_spread",
        side="sell", price=101.0, amount_usd=50.0, trade_id="b", mid_at_fill=100.5,
    )
    store.set_adverse(fill2, 1.5)

    # day 1, strategy avellaneda_stoikov: one resolved fill, one still-pending
    # (adverse_move_usd left NULL) that must be skipped from aggregation.
    fill3 = store.record_fill(
        "s1", 86_400_000 + 1000, "avellaneda_stoikov",
        side="buy", price=200.0, amount_usd=100.0, trade_id="c", mid_at_fill=200.5,
    )
    store.set_adverse(fill3, -2.0)
    store.record_fill(
        "s1", 86_400_000 + 2000, "avellaneda_stoikov",
        side="sell", price=201.0, amount_usd=30.0, trade_id="d", mid_at_fill=200.5,
    )

    result = aggregate_fill_edge_by_day(store.connection, "s1")
    store.close()

    assert set(result.keys()) == {("fixed_spread", 0), ("avellaneda_stoikov", 1)}

    expected_sc_day0 = spread_capture_usd("buy", 100.0, 100.0, 100.5) + spread_capture_usd(
        "sell", 101.0, 50.0, 100.5
    )
    expected_asel_day0 = adverse_selection_usd(
        "buy", 100.0, 100.0, 100.5, 0.0
    ) + adverse_selection_usd("sell", 101.0, 50.0, 100.5, 1.5)
    day0 = result[("fixed_spread", 0)]
    assert day0["fill_count"] == 2
    assert day0["spread_capture_usd"] == pytest.approx(expected_sc_day0)
    assert day0["adverse_selection_usd"] == pytest.approx(expected_asel_day0)

    expected_sc_day1 = spread_capture_usd("buy", 200.0, 100.0, 200.5)
    expected_asel_day1 = adverse_selection_usd("buy", 200.0, 100.0, 200.5, -2.0)
    day1 = result[("avellaneda_stoikov", 1)]
    assert day1["fill_count"] == 1  # the NULL-adverse fill was skipped
    assert day1["spread_capture_usd"] == pytest.approx(expected_sc_day1)
    assert day1["adverse_selection_usd"] == pytest.approx(expected_asel_day1)
