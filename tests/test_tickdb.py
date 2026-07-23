# tests/test_tickdb.py
"""Extraction from raw JSONL lines to kdb-ready row dicts (no pykx needed)."""
import json

from mm_bot.research.tickdb import extract_rows


def _snap(ts, change_id, bids, asks):
    return json.dumps({
        "method": "subscription",
        "params": {"channel": "book.BTC-PERPETUAL.raw",
                   "data": {"type": "snapshot", "change_id": change_id,
                            "timestamp": ts, "bids": [["new", p, a] for p, a in bids],
                            "asks": [["new", p, a] for p, a in asks]}}})


def _chg(ts, change_id, prev, bids=(), asks=()):
    return json.dumps({
        "method": "subscription",
        "params": {"channel": "book.BTC-PERPETUAL.raw",
                   "data": {"type": "change", "change_id": change_id,
                            "prev_change_id": prev, "timestamp": ts,
                            "bids": [list(b) for b in bids],
                            "asks": [list(a) for a in asks]}}})


def _trade(ts, price, amount, direction, trade_id="t1", seq=1):
    return json.dumps({
        "method": "subscription",
        "params": {"channel": "trades.BTC-PERPETUAL.raw",
                   "data": [{"instrument_name": "BTC-PERPETUAL", "trade_id": trade_id,
                             "trade_seq": seq, "timestamp": ts, "price": price,
                             "amount": amount, "direction": direction}]}})


def test_snapshot_emits_top_row():
    lines = [_snap(1000, 1, [(100.0, 5.0), (99.0, 1.0)], [(101.0, 3.0)])]
    rows = list(extract_rows(lines))
    assert rows == [("top", {"tsMs": 1000, "bid": 100.0, "bsize": 5.0,
                             "ask": 101.0, "asize": 3.0})]


def test_change_updates_top():
    lines = [
        _snap(1000, 1, [(100.0, 5.0)], [(101.0, 3.0)]),
        _chg(2000, 2, 1, bids=[("change", 100.0, 7.0)]),
    ]
    rows = list(extract_rows(lines))
    assert rows[-1] == ("top", {"tsMs": 2000, "bid": 100.0, "bsize": 7.0,
                                "ask": 101.0, "asize": 3.0})


def test_one_sided_book_emits_no_top_row():
    lines = [_snap(1000, 1, [(100.0, 5.0)], [])]
    assert list(extract_rows(lines)) == []


def test_trade_emits_trade_row():
    lines = [_trade(1500, 100.5, 250.0, "buy", trade_id="abc", seq=7)]
    assert list(extract_rows(lines)) == [
        ("trade", {"tsMs": 1500, "side": "buy", "price": 100.5,
                   "size": 250.0, "tradeId": "abc", "tradeSeq": 7})]


def test_gap_resets_until_next_snapshot():
    lines = [
        _snap(1000, 1, [(100.0, 5.0)], [(101.0, 3.0)]),
        _chg(2000, 5, 4),                       # prev mismatch -> gap
        _chg(3000, 6, 5),                       # book uninitialized -> still gapped
        _snap(4000, 10, [(102.0, 2.0)], [(103.0, 1.0)]),
    ]
    rows = list(extract_rows(lines))
    assert [r[1]["tsMs"] for r in rows if r[0] == "top"] == [1000, 4000]


def test_non_book_non_trade_messages_are_skipped():
    ticker = json.dumps({
        "method": "subscription",
        "params": {"channel": "ticker.BTC-PERPETUAL.100ms",
                   "data": {"instrument_name": "BTC-PERPETUAL", "timestamp": 1000,
                            "funding_8h": 1e-5, "mark_price": 100.0}}})
    assert list(extract_rows([ticker])) == []
