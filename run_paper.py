# run_paper.py
"""Milestone 2 entrypoint: live paper trading with configured strategies.

Usage: python run_paper.py [config.yaml]
"""
import asyncio
import json
import logging
import subprocess
import sys
import time
from dataclasses import asdict

from mm_bot.config import load_config
from mm_bot.feed.client import DeribitFeedClient
from mm_bot.feed.recorder import JsonlRecorder
from mm_bot.paper.engine import PaperEngine, StrategyLane
from mm_bot.paper.replay import build_strategy
from mm_bot.store.db import Store

log = logging.getLogger("run_paper")

REPORT_INTERVAL_S = 60


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    session_id = time.strftime("%Y%m%d-%H%M%S")
    recorder = JsonlRecorder(cfg.recorder.data_dir, session_id)
    store = Store(cfg.store.db_path)
    store.start_session(
        session_id, int(time.time() * 1000), git_commit(),
        json.dumps({"strategies": [asdict(s) for s in cfg.strategies]}),
    )
    lanes = [
        StrategyLane(
            build_strategy(c), c, store, session_id,
            adverse_horizon_ms=int(cfg.store.adverse_horizon_s * 1000),
        )
        for c in cfg.strategies
    ]
    client = DeribitFeedClient(cfg.feed, on_event=None, on_raw=recorder.record)
    engine = PaperEngine(
        book=client.book, lanes=lanes, store=store, session_id=session_id,
        rollup_interval_ms=cfg.store.rollup_interval_s * 1000,
    )
    client._on_event = engine.on_event  # engine consumes all feed events

    async def report() -> None:
        while True:
            await asyncio.sleep(REPORT_INTERVAL_S)
            mid = client.book.mid()
            for lane in lanes:
                log.info(
                    "%s: pos_usd=%.1f fills=%d quotes=%d equity_usd=%s mid=%s",
                    lane.strategy.name,
                    lane.portfolio.position_usd,
                    lane.portfolio.fill_count,
                    lane.quote_count,
                    f"{lane.portfolio.equity_usd(mid):.4f}" if mid else None,
                    mid,
                )
            recorder.flush()

    log.info("paper session %s (db=%s)", session_id, cfg.store.db_path)
    try:
        await asyncio.gather(client.run(), report())
    finally:
        recorder.close()
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
