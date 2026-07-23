# mm_bot/research/tickdb.py
"""Raw JSONL -> kdb-ready row dicts.

Reuses the exact parser and order book the live engine runs, so the kdb+
tick database sees the market through the same code path as the measured
results; there is no second parse implementation to drift. Gap handling
mirrors mm_bot.paper.replay.replay_file: reset and wait for the next
snapshot.
"""
import json
from collections.abc import Iterable, Iterator

from mm_bot.feed.book import GapError, OrderBook
from mm_bot.feed.messages import BookChange, BookSnapshot, Trade, parse_message


def extract_rows(lines: Iterable[str]) -> Iterator[tuple[str, dict]]:
    book = OrderBook()
    for line in lines:
        for event in parse_message(json.loads(line)):
            if isinstance(event, (BookSnapshot, BookChange)):
                try:
                    if isinstance(event, BookSnapshot):
                        book.apply_snapshot(event)
                    else:
                        book.apply_change(event)
                except GapError:
                    book.reset()
                    continue
                bb, ba = book.best_bid(), book.best_ask()
                if bb is None or ba is None:
                    continue
                yield ("top", {
                    "tsMs": event.timestamp_ms,
                    "bid": bb, "bsize": book.best_bid_size(),
                    "ask": ba, "asize": book.best_ask_size(),
                })
            elif isinstance(event, Trade):
                yield ("trade", {
                    "tsMs": event.timestamp_ms, "side": event.direction,
                    "price": event.price, "size": event.amount,
                    "tradeId": event.trade_id, "tradeSeq": event.trade_seq,
                })
