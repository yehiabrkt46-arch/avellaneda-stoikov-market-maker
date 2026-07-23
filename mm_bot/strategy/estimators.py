# mm_bot/strategy/estimators.py
"""Online estimators feeding the Avellaneda-Stoikov model.

EwmaVolatility: EWMA of squared arithmetic mid changes per second (USD^2/s).
TradeIntensity: order-flow decay parameter k via the exponential-arrival MLE
k = 1 / mean(distance of trades from mid), over a rolling time window.
Both report warm=False until they have enough data; the strategy falls back
to fixed-spread quoting until then.
"""
from collections import deque


class EwmaVolatility:
    def __init__(self, lam: float, min_dt_s: float, min_samples: int) -> None:
        self._lam = lam
        self._min_dt_ms = int(min_dt_s * 1000)
        self._min_samples = min_samples
        self._last_mid: float | None = None
        self._last_ts: int | None = None
        self._v: float | None = None
        self._n = 0

    @property
    def warm(self) -> bool:
        return self._n >= self._min_samples

    def observe(self, mid: float, ts_ms: int) -> None:
        if self._last_ts is None:
            self._last_mid, self._last_ts = mid, ts_ms
            return
        dt_ms = ts_ms - self._last_ts
        if dt_ms < self._min_dt_ms:
            return
        var_sample = (mid - self._last_mid) ** 2 / (dt_ms / 1000.0)
        self._v = var_sample if self._v is None else (
            self._lam * self._v + (1.0 - self._lam) * var_sample
        )
        self._n += 1
        self._last_mid, self._last_ts = mid, ts_ms

    def sigma2(self) -> float | None:
        return self._v if self.warm else None


class TradeIntensity:
    def __init__(self, window_s: float, min_trades: int) -> None:
        self._window_ms = int(window_s * 1000)
        self._min_trades = min_trades
        self._obs: deque = deque()  # (ts_ms, distance_usd)

    def observe(self, distance_usd: float, ts_ms: int) -> None:
        self._obs.append((ts_ms, distance_usd))
        cutoff = ts_ms - self._window_ms
        while self._obs and self._obs[0][0] < cutoff:
            self._obs.popleft()

    @property
    def warm(self) -> bool:
        if len(self._obs) < self._min_trades:
            return False
        return sum(d for _, d in self._obs) > 0.0

    def k(self) -> float | None:
        if not self.warm:
            return None
        mean = sum(d for _, d in self._obs) / len(self._obs)
        return 1.0 / mean


class OfiEstimator:
    """Online order-flow imbalance (Cont, Kukanov, Stoikov 2014).

    Per book update: e = 1[b>=pb]*bs - 1[b<=pb]*pbs - (1[a<=pa]*as - 1[a>=pa]*pas),
    summed over a trailing time window. Mirrors q/ofi.q exactly so the live
    signal is the same quantity the M8 study validated out of sample. No warm
    gate: an empty window reads 0.0, which is a neutral (no-skew) signal.
    """

    def __init__(self, window_ms: int) -> None:
        self._window_ms = window_ms
        self._prev: tuple[float, float, float, float] | None = None
        self._events: deque = deque()  # (ts_ms, e)
        self._sum = 0.0

    def observe(self, bid: float, bsize: float, ask: float, asize: float, ts_ms: int) -> None:
        if self._prev is not None:
            pb, pbs, pa, pas = self._prev
            e = ((bsize if bid >= pb else 0.0) - (pbs if bid <= pb else 0.0)) \
                - ((asize if ask <= pa else 0.0) - (pas if ask >= pa else 0.0))
            self._events.append((ts_ms, e))
            self._sum += e
            cutoff = ts_ms - self._window_ms
            while self._events and self._events[0][0] <= cutoff:
                _, old = self._events.popleft()
                self._sum -= old
        self._prev = (bid, bsize, ask, asize)

    def ofi(self) -> float:
        return self._sum
