# run_testnet_demo.py
"""Supervised Deribit TESTNET order-plumbing demo (milestone 5).

Proves real exchange plumbing works: auth, post-only placement, amend,
cancel, and fill tracking. Its numbers are NEVER reported as performance:
testnet books are not real markets.

Requires environment variables DERIBIT_TESTNET_CLIENT_ID and
DERIBIT_TESTNET_CLIENT_SECRET (create keys at test.deribit.com, free).

Usage: python run_testnet_demo.py
"""
import asyncio
import logging
import os
import time

import websockets

from mm_bot.exec_testnet.orders import OrderTracker, TestnetExecClient
from mm_bot.exec_testnet.rpc import RpcClient

log = logging.getLogger("testnet_demo")

TESTNET_WS = "wss://test.deribit.com/ws/api/v2"
INSTRUMENT = "BTC-PERPETUAL"
AMOUNT_USD = 10.0  # one contract


async def get_mid(rpc: RpcClient) -> float:
    ticker = await rpc.call("public/ticker", {"instrument_name": INSTRUMENT})
    return (ticker["best_bid_price"] + ticker["best_ask_price"]) / 2


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client_id = os.environ.get("DERIBIT_TESTNET_CLIENT_ID")
    client_secret = os.environ.get("DERIBIT_TESTNET_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "Set DERIBIT_TESTNET_CLIENT_ID and DERIBIT_TESTNET_CLIENT_SECRET "
            "(create API keys at test.deribit.com)"
        )

    tracker = OrderTracker()
    async with websockets.connect(TESTNET_WS) as ws:
        rpc = RpcClient(ws, on_notification=tracker.on_notification)
        pump = asyncio.create_task(rpc.read_loop())
        ex = TestnetExecClient(rpc, instrument=INSTRUMENT)

        log.info("authenticating...")
        await rpc.authenticate(client_id, client_secret, now_s=time.monotonic())
        await ex.subscribe_updates()

        mid = await get_mid(rpc)
        log.info("mid=%.1f", mid)

        # 1. passive bid far below mid: must rest, not fill
        far_price = round((mid - 2000) * 2) / 2
        oid = await ex.place_limit("buy", price=far_price, amount_usd=AMOUNT_USD)
        log.info("placed passive bid %s at %.1f", oid, far_price)
        await asyncio.sleep(2)
        log.info("state after 2s: %s (expected open)", tracker.state(oid))

        # 2. amend it 100 USD higher, still far from mid
        await ex.edit(oid, price=far_price + 100, amount_usd=AMOUNT_USD)
        log.info("amended %s to %.1f", oid, far_price + 100)
        await asyncio.sleep(2)

        # 3. cancel it
        await ex.cancel(oid)
        await asyncio.sleep(2)
        log.info("state after cancel: %s (expected cancelled)", tracker.state(oid))

        # 4. crossing bid above mid: should fill immediately (post_only False here)
        cross_price = round((mid + 50) * 2) / 2
        result = await ex._call_with_retry("private/buy", {
            "instrument_name": INSTRUMENT, "amount": AMOUNT_USD,
            "type": "limit", "price": cross_price, "post_only": False,
        })
        oid2 = result["order"]["order_id"]
        log.info("placed crossing bid %s at %.1f", oid2, cross_price)
        await asyncio.sleep(3)
        log.info("state: %s filled_usd=%.1f (expected filled, 10.0)",
                 tracker.state(oid2), tracker.filled_usd(oid2))

        # 5. flatten: sell back whatever filled
        filled = tracker.filled_usd(oid2)
        if filled > 0:
            sell_mid = await get_mid(rpc)
            result = await ex._call_with_retry("private/sell", {
                "instrument_name": INSTRUMENT, "amount": filled,
                "type": "limit", "price": round((sell_mid - 50) * 2) / 2,
                "post_only": False,
            })
            log.info("flattened with sell order %s", result["order"]["order_id"])
            await asyncio.sleep(3)

        pump.cancel()
        log.info("demo complete: auth, place, amend, cancel, fill, flatten all exercised")


if __name__ == "__main__":
    asyncio.run(main())
