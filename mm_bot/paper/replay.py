# mm_bot/paper/replay.py
"""Deterministic replay of a recorded raw JSONL session through the engine."""
import asyncio
import json
from pathlib import Path

from mm_bot.config import StrategyConfig
from mm_bot.feed.book import GapError, OrderBook
from mm_bot.feed.messages import BookChange, BookSnapshot, parse_message
from mm_bot.paper.engine import PaperEngine, StrategyLane
from mm_bot.store.db import Store
from mm_bot.strategy.avellaneda_stoikov import AvellanedaStoikovStrategy
from mm_bot.strategy.fixed_spread import FixedSpreadStrategy


def build_strategy(cfg: StrategyConfig):
    if cfg.kind == "fixed_spread":
        return FixedSpreadStrategy(cfg)
    if cfg.kind == "avellaneda_stoikov":
        return AvellanedaStoikovStrategy(cfg)
    raise ValueError(f"unknown strategy kind: {cfg.kind}")


def replay_file(
    path: str | Path, strategy_cfgs, store: Store, session_id: str,
    adverse_horizon_ms: int = 5000, stale_quote_pull_ms: int = 10_000,
) -> dict:
    store.start_session(session_id, 0, "replay", "{}")
    book = OrderBook()
    lanes = [
        StrategyLane(build_strategy(c), c, store, session_id, adverse_horizon_ms)
        for c in strategy_cfgs
    ]
    engine = PaperEngine(
        book=book, lanes=lanes, store=store, session_id=session_id,
        stale_quote_pull_ms=stale_quote_pull_ms,
    )

    async def _run() -> None:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                for event in parse_message(json.loads(line)):
                    if isinstance(event, (BookSnapshot, BookChange)):
                        try:
                            engine.apply_book_event(event)
                        except GapError:
                            book.reset()
                            continue
                    await engine.on_event(event)

    asyncio.run(_run())
    mid = book.mid()
    return {
        lane.strategy.name: {
            "fills": lane.portfolio.fill_count,
            "quotes": lane.quote_count,
            "position_usd": lane.portfolio.position_usd,
            "equity_btc": lane.portfolio.equity_btc(mid) if mid else None,
        }
        for lane in lanes
    }
