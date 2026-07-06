# mm_bot/strategy/fixed_spread.py
"""Baseline: symmetric quotes at a fixed half-spread around mid."""
from mm_bot.config import StrategyConfig
from mm_bot.strategy.base import QuotePair, Strategy, round_to_tick


class FixedSpreadStrategy(Strategy):
    def __init__(self, cfg: StrategyConfig) -> None:
        self._cfg = cfg
        self.name = cfg.name

    def quotes(self, mid: float, position_usd: float, now_ms: int) -> QuotePair:
        h = self._cfg.half_spread_usd
        tick = self._cfg.tick_size
        return QuotePair(
            bid=round_to_tick(mid - h, tick, down=True),
            ask=round_to_tick(mid + h, tick, down=False),
        )
