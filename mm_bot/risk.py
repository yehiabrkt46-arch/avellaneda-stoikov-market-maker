# mm_bot/risk.py
"""Per-lane risk management: inventory cap and drawdown kill switch.

Applied to every quote decision after the strategy computes its desired
quotes, before they reach the fill simulator. Two independent guards:

- Inventory cap (`inventory_cap_usd`): once position_usd reaches the cap on
  either side, the side that would grow the position further is suppressed
  (bid suppressed when long at the cap, ask suppressed when short at the
  cap). The unloading side stays quoted so the lane can trade back toward
  flat. A `cap_bind` event fires once when the cap first binds and a
  `cap_unbind` event fires once when it releases, not on every quote.
- Drawdown kill switch (`max_drawdown_usd`): tracks the peak equity_usd seen
  so far this session. If the drop from that peak exceeds the limit, the
  lane stops quoting for the rest of the session (both sides None,
  permanently). There is no synthetic flatten fill: the terminal open
  position and its mark-to-mid equity are reported as-is and disclosed.
  A synthetic mid fill would pollute the strict-cross fill methodology, so
  this is a deliberate deviation from a naive "flattens" reading of the
  spec, in favor of fill-methodology purity.

Both guards emit events via the injected callback so the run write-up can
disclose exactly when and why quoting was suppressed.
"""
from mm_bot.config import StrategyConfig
from mm_bot.strategy.base import QuotePair

_NONE_QUOTES = QuotePair(bid=None, ask=None)


class RiskManager:
    def __init__(self, cfg: StrategyConfig, on_event) -> None:
        self._cap = cfg.inventory_cap_usd
        self._max_drawdown = cfg.max_drawdown_usd
        self._on_event = on_event  # sync callable(kind: str, detail: str, ts_ms: int)
        self._cap_bound_side: str | None = None
        self._peak_equity_usd: float | None = None
        self._killed = False

    @property
    def killed(self) -> bool:
        return self._killed

    def filter_quotes(
        self, q: QuotePair, position_usd: float, equity_usd: float | None, ts_ms: int
    ) -> QuotePair:
        if self._killed:
            return _NONE_QUOTES
        if equity_usd is not None:
            self._check_drawdown(equity_usd, position_usd, ts_ms)
            if self._killed:
                return _NONE_QUOTES
        return self._apply_cap(q, position_usd, ts_ms)

    def _check_drawdown(self, equity_usd: float, position_usd: float, ts_ms: int) -> None:
        if self._peak_equity_usd is None or equity_usd > self._peak_equity_usd:
            self._peak_equity_usd = equity_usd
            return
        if self._peak_equity_usd - equity_usd > self._max_drawdown:
            self._killed = True
            self._on_event(
                "kill_switch",
                f"peak_equity_usd={self._peak_equity_usd:.4f} "
                f"equity_usd={equity_usd:.4f} position_usd={position_usd:.2f}",
                ts_ms,
            )

    def _apply_cap(self, q: QuotePair, position_usd: float, ts_ms: int) -> QuotePair:
        bid, ask = q.bid, q.ask
        bound_side = None
        if position_usd >= self._cap:
            bound_side = "bid"
            bid = None
        elif position_usd <= -self._cap:
            bound_side = "ask"
            ask = None

        if bound_side is not None and self._cap_bound_side != bound_side:
            self._cap_bound_side = bound_side
            self._on_event(
                "cap_bind", f"side={bound_side} position_usd={position_usd:.2f}", ts_ms
            )
        elif bound_side is None and self._cap_bound_side is not None:
            self._on_event(
                "cap_unbind",
                f"side={self._cap_bound_side} position_usd={position_usd:.2f}",
                ts_ms,
            )
            self._cap_bound_side = None

        return QuotePair(bid=bid, ask=ask)
