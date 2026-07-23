"""Avellaneda-Stoikov optimal market making.

Reservation price r = mid - q*gamma*sigma2*tau shifts quotes against
inventory; optimal half-spread delta = gamma*sigma2*tau/2 +
(1/gamma)*ln(1 + gamma/k) trades spread capture against fill probability.

tau is CONSTANT (stationary approximation): a perpetual has no terminal
time, so the classic shrinking horizon would force artificial end-of-session
inventory dumping. This is the documented deviation from the finite-horizon
textbook model.

Until both estimators are warm the strategy quotes a fixed spread
(half_spread_usd), identical to the baseline, so early quotes never come
from unconverged estimates.
"""
import math

from mm_bot.config import StrategyConfig
from mm_bot.strategy.base import QuotePair, Strategy, round_to_tick
from mm_bot.strategy.estimators import EwmaVolatility, OfiEstimator, TradeIntensity


class AvellanedaStoikovStrategy(Strategy):
    def __init__(self, cfg: StrategyConfig) -> None:
        self._cfg = cfg
        self.name = cfg.name
        self._vol = EwmaVolatility(
            lam=cfg.vol_lambda, min_dt_s=cfg.vol_min_dt_s,
            min_samples=cfg.vol_min_samples,
        )
        self._intensity = TradeIntensity(
            window_s=cfg.k_window_s, min_trades=cfg.k_min_trades,
        )
        self._ofi = OfiEstimator(window_ms=int(cfg.ofi_window_s * 1000))
        self._last_mid: float | None = None

    def observe_mid(self, mid: float, ts_ms: int) -> None:
        self._last_mid = mid
        self._vol.observe(mid, ts_ms)

    def observe_trade(self, price: float, ts_ms: int) -> None:
        if self._last_mid is None:
            return
        self._intensity.observe(abs(price - self._last_mid), ts_ms)

    def observe_book(self, bid, bid_size, ask, ask_size, ts_ms) -> None:
        self._ofi.observe(bid, bid_size, ask, ask_size, ts_ms)

    def quotes(self, mid: float, position_usd: float, now_ms: int) -> QuotePair:
        tick = self._cfg.tick_size
        sigma2 = self._vol.sigma2()
        k = self._intensity.k()
        if sigma2 is None or k is None:
            h = self._cfg.half_spread_usd  # warmup fallback = baseline behavior
            return QuotePair(
                bid=round_to_tick(mid - h, tick, down=True),
                ask=round_to_tick(mid + h, tick, down=False),
            )
        gamma = self._cfg.gamma
        tau = self._cfg.horizon_s
        q = position_usd / self._cfg.quote_size_usd
        reservation = mid - q * gamma * sigma2 * tau
        if self._cfg.ofi_scale != 0.0:
            reservation += self._cfg.ofi_scale * self._cfg.ofi_beta * self._ofi.ofi() * mid
        half = gamma * sigma2 * tau / 2.0 + (1.0 / gamma) * math.log(1.0 + gamma / k)
        return QuotePair(
            bid=round_to_tick(reservation - half, tick, down=True),
            ask=round_to_tick(reservation + half, tick, down=False),
        )
