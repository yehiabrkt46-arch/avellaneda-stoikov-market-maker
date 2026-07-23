import pytest

from mm_bot.feed.book import GapError, OrderBook
from mm_bot.feed.messages import BookChange, BookSnapshot


def snapshot():
    return BookSnapshot(
        instrument="BTC-PERPETUAL",
        change_id=1000,
        timestamp_ms=1751800000000,
        bids=[(60000.0, 5000.0), (59999.5, 300.0)],
        asks=[(60000.5, 4200.0), (60001.0, 900.0)],
    )


def change(change_id, prev_change_id, bids=(), asks=()):
    return BookChange(
        instrument="BTC-PERPETUAL",
        change_id=change_id,
        prev_change_id=prev_change_id,
        timestamp_ms=1751800000100,
        bids=list(bids),
        asks=list(asks),
    )


def test_snapshot_initializes_book():
    book = OrderBook()
    assert not book.initialized
    book.apply_snapshot(snapshot())
    assert book.initialized
    assert book.best_bid() == 60000.0
    assert book.best_ask() == 60000.5
    assert book.mid() == 60000.25
    assert book.change_id == 1000


def test_change_updates_levels():
    book = OrderBook()
    book.apply_snapshot(snapshot())
    book.apply_change(
        change(
            1001,
            1000,
            bids=[("change", 60000.0, 4500.0), ("delete", 59999.5, 0.0)],
            asks=[("new", 60000.4, 50.0)],
        )
    )
    assert book.best_bid() == 60000.0
    assert book.best_ask() == 60000.4
    assert book.change_id == 1001


def test_delete_best_bid_promotes_next_level():
    book = OrderBook()
    book.apply_snapshot(snapshot())
    book.apply_change(change(1001, 1000, bids=[("delete", 60000.0, 0.0)]))
    assert book.best_bid() == 59999.5


def test_gap_raises():
    book = OrderBook()
    book.apply_snapshot(snapshot())
    with pytest.raises(GapError):
        book.apply_change(change(1002, 1001))  # skips change_id 1001


def test_change_before_snapshot_raises():
    book = OrderBook()
    with pytest.raises(GapError):
        book.apply_change(change(1001, 1000))


def test_reset_clears_book():
    book = OrderBook()
    book.apply_snapshot(snapshot())
    book.reset()
    assert not book.initialized
    assert book.best_bid() is None
    assert book.best_ask() is None
    assert book.mid() is None


def test_snapshot_after_reset_reinitializes():
    book = OrderBook()
    book.apply_snapshot(snapshot())
    book.reset()
    book.apply_snapshot(snapshot())
    assert book.best_bid() == 60000.0


def test_best_size_accessors():
    book = OrderBook()
    book.apply_snapshot(BookSnapshot(instrument="BTC-PERPETUAL", change_id=1,
                                     timestamp_ms=1000,
                                     bids=[(100.0, 5.0)], asks=[(101.0, 3.0)]))
    assert book.best_bid_size() == 5.0
    assert book.best_ask_size() == 3.0
