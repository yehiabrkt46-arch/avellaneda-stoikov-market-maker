# run_recorder.py
"""Milestone 1 entrypoint: maintain a live book and record raw messages.

Usage: python run_recorder.py [config.yaml]
"""
import asyncio
import logging
import sys
import time

from mm_bot.config import load_config
from mm_bot.feed.client import DeribitFeedClient
from mm_bot.feed.messages import BookChange, BookSnapshot, Trade
from mm_bot.feed.recorder import JsonlRecorder

log = logging.getLogger("run_recorder")

REPORT_INTERVAL_S = 60


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    session_id = time.strftime("%Y%m%d-%H%M%S")
    recorder = JsonlRecorder(cfg.recorder.data_dir, session_id)
    stats = {"snapshots": 0, "changes": 0, "trades": 0}

    async def on_event(event) -> None:
        match event:
            case BookSnapshot():
                stats["snapshots"] += 1
            case BookChange():
                stats["changes"] += 1
            case Trade():
                stats["trades"] += 1

    client = DeribitFeedClient(cfg.feed, on_event, on_raw=recorder.record)

    async def report() -> None:
        while True:
            await asyncio.sleep(REPORT_INTERVAL_S)
            book = client.book
            log.info(
                "stats=%s best_bid=%s best_ask=%s mid=%s book_ts=%s",
                stats,
                book.best_bid(),
                book.best_ask(),
                book.mid(),
                book.timestamp_ms,
            )
            recorder.flush()

    log.info("recording session %s to %s", session_id, recorder.path)
    try:
        await asyncio.gather(client.run(), report())
    finally:
        recorder.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
