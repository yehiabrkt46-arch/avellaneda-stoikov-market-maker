# tests/test_replay.py
import json

import pytest

from mm_bot.config import StrategyConfig
from mm_bot.paper.engine import funding_accrual_btc
from mm_bot.paper.replay import replay_file
from mm_bot.store.db import Store


def msg_snapshot(change_id, ts, bid, ask):
    return {
        "jsonrpc": "2.0", "method": "subscription",
        "params": {"channel": "book.BTC-PERPETUAL.100ms", "data": {
            "type": "snapshot", "timestamp": ts, "instrument_name": "BTC-PERPETUAL",
            "change_id": change_id,
            "bids": [["new", bid, 5000.0]], "asks": [["new", ask, 4200.0]],
        }},
    }


def msg_trade(ts, price, amount, trade_id):
    return {
        "jsonrpc": "2.0", "method": "subscription",
        "params": {"channel": "trades.BTC-PERPETUAL.100ms", "data": [{
            "instrument_name": "BTC-PERPETUAL", "trade_id": trade_id,
            "trade_seq": 1, "timestamp": ts, "price": price,
            "amount": amount, "direction": "sell",
        }]},
    }


def msg_ticker(ts, funding_8h, mark_price):
    return {
        "jsonrpc": "2.0", "method": "subscription",
        "params": {"channel": "ticker.BTC-PERPETUAL.100ms", "data": {
            "instrument_name": "BTC-PERPETUAL", "timestamp": ts,
            "funding_8h": funding_8h, "mark_price": mark_price,
        }},
    }


def write_jsonl(path, messages):
    with open(path, "w", encoding="utf-8") as fh:
        for m in messages:
            fh.write(json.dumps(m) + "\n")


def test_replay_produces_deterministic_fills(tmp_path):
    raw = tmp_path / "raw.jsonl"
    write_jsonl(raw, [
        msg_snapshot(1000, ts=1_000_000, bid=60000.0, ask=60000.5),
        # mid 60000.25; half spread 5 -> bid 59995.0 / ask 60005.5
        msg_trade(ts=1_000_100, price=59990.0, amount=50.0, trade_id="a"),  # fills bid
        msg_trade(ts=1_000_200, price=60010.0, amount=30.0, trade_id="b"),  # fills ask
    ])
    cfg = StrategyConfig(name="base", half_spread_usd=5.0, quote_size_usd=100.0)
    store = Store(tmp_path / "mm.sqlite")
    summary = replay_file(raw, [cfg], store, session_id="replay-1")
    rows = store.connection.execute(
        "SELECT side, price, amount_usd FROM fills ORDER BY id"
    ).fetchall()
    assert rows == [("buy", 59995.0, 50.0), ("sell", 60005.5, 30.0)]
    assert summary["base"]["fills"] == 2
    assert summary["base"]["position_usd"] == 20.0
    store.close()


def test_replay_is_reproducible(tmp_path):
    raw = tmp_path / "raw.jsonl"
    write_jsonl(raw, [
        msg_snapshot(1000, ts=1_000_000, bid=60000.0, ask=60000.5),
        msg_trade(ts=1_000_100, price=59990.0, amount=50.0, trade_id="a"),
    ])
    cfg = StrategyConfig(name="base")
    s1 = Store(tmp_path / "a.sqlite")
    s2 = Store(tmp_path / "b.sqlite")
    r1 = replay_file(raw, [cfg], s1, session_id="r1")
    r2 = replay_file(raw, [cfg], s2, session_id="r2")
    assert r1 == r2
    s1.close()
    s2.close()


def test_replay_with_ticker_accrues_funding_deterministically(tmp_path):
    raw = tmp_path / "raw.jsonl"
    write_jsonl(raw, [
        msg_snapshot(1000, ts=1_000_000, bid=60000.0, ask=60000.5),
        msg_ticker(ts=1_000_100, funding_8h=0.0002, mark_price=60000.0),
        msg_trade(ts=1_000_200, price=59990.0, amount=50.0, trade_id="a"),  # fills bid, 50 long
        msg_snapshot(1001, ts=1_061_000, bid=60000.0, ask=60000.5),  # 61s later -> rollup fires
    ])
    cfg = StrategyConfig(name="base", half_spread_usd=5.0, quote_size_usd=100.0)
    s1 = Store(tmp_path / "a.sqlite")
    s2 = Store(tmp_path / "b.sqlite")
    r1 = replay_file(raw, [cfg], s1, session_id="r1")
    r2 = replay_file(raw, [cfg], s2, session_id="r2")
    assert r1 == r2  # replaying the same recording twice is deterministic
    expected = funding_accrual_btc(position_usd=50.0, mark=60000.0, funding_8h=0.0002, elapsed_s=61.0)
    assert r1["base"]["funding_btc"] == pytest.approx(expected)
    row = s1.connection.execute("SELECT funding_btc FROM rollups").fetchone()
    assert row[0] == pytest.approx(expected)
    s1.close()
    s2.close()


def test_replay_without_ticker_keeps_funding_zero(tmp_path):
    # old recordings predating the ticker channel have no Ticker messages at
    # all; funding must stay 0, matching pre-milestone-4.5 behavior.
    raw = tmp_path / "raw.jsonl"
    write_jsonl(raw, [
        msg_snapshot(1000, ts=1_000_000, bid=60000.0, ask=60000.5),
        msg_trade(ts=1_000_200, price=59990.0, amount=50.0, trade_id="a"),
        msg_snapshot(1001, ts=1_061_000, bid=60000.0, ask=60000.5),
    ])
    cfg = StrategyConfig(name="base", half_spread_usd=5.0, quote_size_usd=100.0)
    store = Store(tmp_path / "mm.sqlite")
    summary = replay_file(raw, [cfg], store, session_id="replay-old")
    assert summary["base"]["funding_btc"] == 0.0
    row = store.connection.execute("SELECT funding_btc FROM rollups").fetchone()
    assert row[0] == 0.0
    store.close()
