# mm_bot/strategy/base.py
"""Strategy interface: mid + inventory + exchange time in, desired quotes out."""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class QuotePair:
    bid: float | None
    ask: float | None


def round_to_tick(price: float, tick: float, down: bool) -> float:
    n = price / tick
    rounded = math.floor(n) if down else math.ceil(n)
    return rounded * tick


class Strategy:
    """Base strategy: engine lanes feed these hooks; override as needed."""

    name = "strategy"

    def observe_mid(self, mid: float, ts_ms: int) -> None:
        pass

    def observe_trade(self, price: float, ts_ms: int) -> None:
        pass

    def quotes(self, mid: float, position_usd: float, now_ms: int):
        raise NotImplementedError
