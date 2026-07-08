# mm-bot Milestone 3: Avellaneda-Stoikov Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Model-driven quoting: Avellaneda-Stoikov reservation price and optimal spread, fed by an online EWMA volatility estimator and an order-flow intensity (k) estimator calibrated from real trade distances, running as a second lane beside the FixedSpread baseline.

**Architecture:** Strategies gain no-op `observe_mid`/`observe_trade` hooks that the engine lanes call; A-S overrides them to feed its estimators. Until both estimators are warm, A-S falls back to fixed-spread quoting (never quotes off garbage estimates). Constant time-to-horizon tau (stationary approximation): a perpetual has no terminal time, so the classic shrinking (T - t) would cause artificial end-of-session inventory dumping; documented in code and README.

**Tech Stack:** Python stdlib (math, collections.deque). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-06-mm-bot-design.md` (horizon wording amended by this plan's Task 5). Prerequisite: milestones 1-2 complete, 61 tests green.

**Model facts the engineer needs:**
- Reservation price: `r = mid - q * gamma * sigma2 * tau`, where q is inventory in quote-clips (`position_usd / quote_size_usd`), gamma is risk aversion (units 1/USD), sigma2 is mid variance in USD^2 per second, tau is the constant horizon in seconds.
- Optimal half-spread: `delta = gamma * sigma2 * tau / 2 + (1 / gamma) * ln(1 + gamma / k)`, k in 1/USD.
- Quotes: bid = round-down-to-tick(r - delta), ask = round-up-to-tick(r + delta).
- Volatility: EWMA of squared arithmetic mid changes normalized by elapsed time. On each accepted sample (at least `vol_min_dt_s` apart): `var_sample = (mid - last_mid)^2 / dt_s`; `v = lam * v + (1 - lam) * var_sample` (v initialized to the first sample). Warm after `vol_min_samples` samples.
- Intensity: trade arrival rate vs distance from mid is modeled as `A * exp(-k * dist)`; the MLE for k over observed distances is `k = 1 / mean(dist)` where `dist = |trade_price - mid_at_trade|`. Rolling time window `k_window_s`, warm after `k_min_trades` trades in window and mean > 0.
- Hand-computed reference case used in tests: sigma2 = 4 USD^2/s, tau = 60 s, gamma = 0.01 /USD, k = 0.05 /USD, mid = 60000, q = +2:
  - shift = 2 * 0.01 * 4 * 60 = 4.8, so r = 59995.2
  - delta = 0.01*4*60/2 + (1/0.01)*ln(1 + 0.01/0.05) = 1.2 + 100*ln(1.2) = 1.2 + 18.2321557 = 19.4321557
  - bid = 59975.7678 -> 59975.5 (tick 0.5, down), ask = 60014.6322 -> 60015.0 (up)

---

### Task 1: Config fields for A-S

**Files:**
- Modify: `mm_bot/config.py` (extend StrategyConfig)
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add failing test** (append to `tests/test_config.py`)

```python
def test_strategy_config_as_fields_default(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("")
    s = load_config(p).strategies[0]
    assert s.gamma == 0.001
    assert s.horizon_s == 60.0
    assert s.vol_lambda == 0.97
    assert s.vol_min_dt_s == 1.0
    assert s.vol_min_samples == 30
    assert s.k_window_s == 1800.0
    assert s.k_min_trades == 50
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_config.py -v`
Expected: new test FAILS with AttributeError, 4 old pass.

- [ ] **Step 3: Implement** — add these fields to `StrategyConfig` in `mm_bot/config.py` (after `requote_interval_s`):

```python
    gamma: float = 0.001
    horizon_s: float = 60.0
    vol_lambda: float = 0.97
    vol_min_dt_s: float = 1.0
    vol_min_samples: int = 30
    k_window_s: float = 1800.0
    k_min_trades: int = 50
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_config.py -v` — expected: 5 passed. Full suite: 62 passed.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/config.py tests/test_config.py
git commit -m "feat: Avellaneda-Stoikov config parameters"
```

---

### Task 2: Online estimators (volatility + intensity)

**Files:**
- Create: `mm_bot/strategy/estimators.py`
- Test: `tests/test_estimators.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_estimators.py
import pytest

from mm_bot.strategy.estimators import EwmaVolatility, TradeIntensity


def test_vol_not_warm_before_min_samples():
    vol = EwmaVolatility(lam=0.5, min_dt_s=1.0, min_samples=3)
    vol.observe(60000.0, 1_000_000)
    vol.observe(60002.0, 1_001_000)  # 1 sample (first obs is just the anchor)
    assert not vol.warm
    assert vol.sigma2() is None


def test_vol_constant_diffs_converge_to_known_variance():
    vol = EwmaVolatility(lam=0.5, min_dt_s=1.0, min_samples=3)
    ts = 1_000_000
    vol.observe(60000.0, ts)
    for i in range(1, 6):  # +2.0 USD exactly every 1s -> var_sample = 4 every time
        vol.observe(60000.0 + 2.0 * i, ts + 1000 * i)
    assert vol.warm
    assert vol.sigma2() == pytest.approx(4.0)


def test_vol_ignores_samples_closer_than_min_dt():
    vol = EwmaVolatility(lam=0.5, min_dt_s=1.0, min_samples=2)
    vol.observe(60000.0, 1_000_000)
    vol.observe(70000.0, 1_000_100)  # 0.1s later: ignored entirely
    vol.observe(60002.0, 1_001_000)
    vol.observe(60004.0, 1_002_000)
    assert vol.sigma2() == pytest.approx(4.0)


def test_intensity_mle_is_inverse_mean_distance():
    k = TradeIntensity(window_s=3600.0, min_trades=3)
    k.observe(10.0, 1_000_000)
    k.observe(20.0, 1_001_000)
    k.observe(30.0, 1_002_000)
    assert k.warm
    assert k.k() == pytest.approx(1.0 / 20.0)


def test_intensity_not_warm_below_min_trades():
    k = TradeIntensity(window_s=3600.0, min_trades=3)
    k.observe(10.0, 1_000_000)
    k.observe(20.0, 1_001_000)
    assert not k.warm
    assert k.k() is None


def test_intensity_evicts_outside_window():
    k = TradeIntensity(window_s=10.0, min_trades=2)
    k.observe(100.0, 1_000_000)
    k.observe(10.0, 1_020_000)  # first trade now 20s old, window 10s -> evicted
    k.observe(30.0, 1_021_000)
    assert k.k() == pytest.approx(1.0 / 20.0)  # mean of (10, 30) only


def test_intensity_zero_mean_not_warm():
    k = TradeIntensity(window_s=3600.0, min_trades=2)
    k.observe(0.0, 1_000_000)
    k.observe(0.0, 1_001_000)
    assert k.k() is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_estimators.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_estimators.py -v` — expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/strategy/estimators.py tests/test_estimators.py
git commit -m "feat: online EWMA volatility and trade intensity estimators"
```

---

### Task 3: Strategy observation hooks + engine wiring

**Files:**
- Modify: `mm_bot/strategy/base.py` (add Strategy base class with no-op hooks)
- Modify: `mm_bot/strategy/fixed_spread.py` (inherit Strategy)
- Modify: `mm_bot/paper/engine.py` (lanes call the hooks)
- Test: `tests/test_engine_hooks.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_engine_hooks.py
from mm_bot.config import StrategyConfig
from mm_bot.feed.book import OrderBook
from mm_bot.feed.messages import BookSnapshot, Trade
from mm_bot.paper.engine import PaperEngine, StrategyLane
from mm_bot.store.db import Store
from mm_bot.strategy.base import QuotePair, Strategy


class SpyStrategy(Strategy):
    def __init__(self):
        self.name = "spy"
        self.mids = []
        self.trades = []

    def observe_mid(self, mid, ts_ms):
        self.mids.append((mid, ts_ms))

    def observe_trade(self, price, ts_ms):
        self.trades.append((price, ts_ms))

    def quotes(self, mid, position_usd, now_ms):
        return QuotePair(bid=None, ask=None)


def snapshot(ts):
    return BookSnapshot(
        instrument="BTC-PERPETUAL", change_id=1000, timestamp_ms=ts,
        bids=[(60000.0, 5000.0)], asks=[(60000.5, 4200.0)],
    )


def trade_ev(ts, price):
    return Trade(
        instrument="BTC-PERPETUAL", trade_id="t1", trade_seq=1,
        timestamp_ms=ts, price=price, amount=10.0, direction="sell",
    )


async def test_lane_feeds_strategy_hooks(tmp_path):
    cfg = StrategyConfig(name="spy")
    store = Store(tmp_path / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    strat = SpyStrategy()
    lane = StrategyLane(strat, cfg, store, "s1", adverse_horizon_ms=5000)
    engine = PaperEngine(book=OrderBook(), lanes=[lane], store=store, session_id="s1")
    engine.apply_book_event(snapshot(ts=1_000_000))
    await engine.on_event(snapshot(ts=1_000_000))
    await engine.on_event(trade_ev(ts=1_000_100, price=59990.0))
    assert strat.mids == [(60000.25, 1_000_000)]
    assert strat.trades == [(59990.0, 1_000_100)]
    store.close()


def test_base_strategy_hooks_are_noops():
    s = Strategy()
    s.observe_mid(60000.0, 0)   # must not raise
    s.observe_trade(60000.0, 0)  # must not raise
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_engine_hooks.py -v`
Expected: ImportError (`Strategy` not in base).

- [ ] **Step 3: Implement**

Add to `mm_bot/strategy/base.py` (below `round_to_tick`):

```python
class Strategy:
    """Base strategy: engine lanes feed these hooks; override as needed."""

    name = "strategy"

    def observe_mid(self, mid: float, ts_ms: int) -> None:
        pass

    def observe_trade(self, price: float, ts_ms: int) -> None:
        pass

    def quotes(self, mid: float, position_usd: float, now_ms: int):
        raise NotImplementedError
```

In `mm_bot/strategy/fixed_spread.py`, change the import and class line to inherit:

```python
from mm_bot.strategy.base import QuotePair, Strategy, round_to_tick


class FixedSpreadStrategy(Strategy):
```

(rest unchanged)

In `mm_bot/paper/engine.py`:
- In `StrategyLane.on_mid`, insert `self.strategy.observe_mid(mid, ts_ms)` as the FIRST line (before `self._current_mid = mid`).
- In `StrategyLane.on_trade`, after the `if self._current_mid is None: return` guard, insert `self.strategy.observe_trade(trade.price, trade.timestamp_ms)` before `self.sim.on_trade(trade)`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_engine_hooks.py -v` — expected: 2 passed.
Full suite: `.venv\Scripts\python -m pytest -q` — expected: all green, no regressions in test_engine.py or test_fixed_spread.py.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/strategy/base.py mm_bot/strategy/fixed_spread.py mm_bot/paper/engine.py tests/test_engine_hooks.py
git commit -m "feat: strategy observation hooks wired through engine lanes"
```

---

### Task 4: Avellaneda-Stoikov strategy

**Files:**
- Create: `mm_bot/strategy/avellaneda_stoikov.py`
- Modify: `mm_bot/paper/replay.py` (build_strategy factory)
- Modify: `config.yaml` (add A-S lane)
- Test: `tests/test_avellaneda_stoikov.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_avellaneda_stoikov.py
import pytest

from mm_bot.config import StrategyConfig
from mm_bot.paper.replay import build_strategy
from mm_bot.strategy.avellaneda_stoikov import AvellanedaStoikovStrategy


def make_strat(**over):
    cfg = StrategyConfig(
        kind="avellaneda_stoikov", name="as", half_spread_usd=5.0,
        quote_size_usd=100.0, tick_size=0.5,
        gamma=0.01, horizon_s=60.0,
        vol_lambda=0.5, vol_min_dt_s=1.0, vol_min_samples=3,
        k_window_s=3600.0, k_min_trades=3, **over,
    )
    return AvellanedaStoikovStrategy(cfg)


def warm_up(strat, mid=60000.0):
    ts = 1_000_000
    strat.observe_mid(mid, ts)
    for i in range(1, 6):  # +2 USD per 1s -> sigma2 = 4
        strat.observe_mid(mid + 2.0 * i, ts + 1000 * i)
    last_mid = mid + 10.0  # after warmup loop the last observed mid is 60010
    for d in (20.0, 20.0, 20.0):  # mean distance 20 -> k = 0.05
        strat.observe_trade(last_mid + d, ts + 5000)
    return ts + 6000


def test_falls_back_to_fixed_spread_until_warm():
    strat = make_strat()
    q = strat.quotes(mid=60000.25, position_usd=0.0, now_ms=0)
    assert q.bid == 59995.0  # same as FixedSpread with half_spread 5
    assert q.ask == 60005.5


def test_warm_quotes_match_hand_computed_reference():
    strat = make_strat()
    now = warm_up(strat)
    # sigma2=4, tau=60, gamma=0.01, k=0.05, mid=60000, q=+2 (200 USD / 100 USD clip)
    # r = 60000 - 2*0.01*4*60 = 59995.2
    # delta = 1.2 + 100*ln(1.2) = 19.4321557
    q = strat.quotes(mid=60000.0, position_usd=200.0, now_ms=now)
    assert q.bid == 59975.5   # 59975.7678 floored to 0.5 tick
    assert q.ask == 60015.0   # 60014.6322 ceiled


def test_zero_inventory_quotes_are_symmetric_around_mid():
    strat = make_strat()
    now = warm_up(strat)
    q = strat.quotes(mid=60000.0, position_usd=0.0, now_ms=now)
    assert q.bid == pytest.approx(60000.0 - 19.5, abs=0.51)
    assert q.ask == pytest.approx(60000.0 + 19.5, abs=0.51)


def test_long_inventory_skews_quotes_down():
    strat = make_strat()
    now = warm_up(strat)
    flat = strat.quotes(mid=60000.0, position_usd=0.0, now_ms=now)
    long = strat.quotes(mid=60000.0, position_usd=300.0, now_ms=now)
    assert long.bid < flat.bid   # less eager to buy more
    assert long.ask < flat.ask   # more eager to sell


def test_observe_trade_uses_distance_from_last_mid():
    strat = make_strat(k_min_trades=1)
    strat.observe_mid(60000.0, 1_000_000)
    strat.observe_trade(60030.0, 1_000_500)  # distance 30
    assert strat._intensity.k() == pytest.approx(1.0 / 30.0)


def test_build_strategy_factory_dispatches():
    cfg = StrategyConfig(kind="avellaneda_stoikov", name="as")
    assert isinstance(build_strategy(cfg), AvellanedaStoikovStrategy)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_avellaneda_stoikov.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
# mm_bot/strategy/avellaneda_stoikov.py
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
from mm_bot.strategy.estimators import EwmaVolatility, TradeIntensity


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
        self._last_mid: float | None = None

    def observe_mid(self, mid: float, ts_ms: int) -> None:
        self._last_mid = mid
        self._vol.observe(mid, ts_ms)

    def observe_trade(self, price: float, ts_ms: int) -> None:
        if self._last_mid is None:
            return
        self._intensity.observe(abs(price - self._last_mid), ts_ms)

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
        half = gamma * sigma2 * tau / 2.0 + (1.0 / gamma) * math.log(1.0 + gamma / k)
        return QuotePair(
            bid=round_to_tick(reservation - half, tick, down=True),
            ask=round_to_tick(reservation + half, tick, down=False),
        )
```

In `mm_bot/paper/replay.py`, update `build_strategy` (add the import at the top with the others):

```python
from mm_bot.strategy.avellaneda_stoikov import AvellanedaStoikovStrategy


def build_strategy(cfg: StrategyConfig):
    if cfg.kind == "fixed_spread":
        return FixedSpreadStrategy(cfg)
    if cfg.kind == "avellaneda_stoikov":
        return AvellanedaStoikovStrategy(cfg)
    raise ValueError(f"unknown strategy kind: {cfg.kind}")
```

Update `config.yaml` strategies section to run both lanes:

```yaml
strategies:
  - kind: fixed_spread
    name: fixed_spread
    half_spread_usd: 5.0
    quote_size_usd: 100.0
  - kind: avellaneda_stoikov
    name: avellaneda_stoikov
    half_spread_usd: 5.0
    quote_size_usd: 100.0
    gamma: 0.001
    horizon_s: 60.0
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_avellaneda_stoikov.py -v` — expected: 6 passed.
Full suite — expected: all green.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/strategy/avellaneda_stoikov.py mm_bot/paper/replay.py config.yaml tests/test_avellaneda_stoikov.py
git commit -m "feat: Avellaneda-Stoikov strategy with estimator warmup fallback"
```

---

### Task 5: Spec amendment + live verification (Fable, not a subagent)

Performed by the main session. Subagents stop after Task 4.

- [ ] **Step 1: Amend spec horizon wording**

In `docs/superpowers/specs/2026-07-06-mm-bot-design.md`, replace the
"Horizon handling" bullet with: constant time-to-horizon tau (stationary
approximation), rationale: perpetual contract has no terminal time; shrinking
horizon causes artificial end-of-session inventory dumping. Commit.

- [ ] **Step 2: Replay real recording with both lanes**

Replay the recorded raw file through `replay_file` with the two-lane config.
Expected: both lanes report quotes; A-S lane's early quotes use fallback
spread (on a short file it may never warm, which is acceptable). Verify no
errors and the baseline lane matches its earlier single-lane replay numbers.

- [ ] **Step 3: Live paper run (~10 minutes)**

Run `run_paper.py` with both lanes. Expected: two stats lines per minute; A-S
lane transitions from fallback to model quotes once vol (30 samples) and k
(50 trades) warm; quote widths differ from baseline after warmup; no
tracebacks.

- [ ] **Step 4: Milestone gate**

Full suite green, replay deterministic, live run stable. Then milestone 3 done.

---

## Execution notes

- Sonnet 5 subagents implement Tasks 1-4 sequentially. Fable reviews each diff and performs Task 5.
- gamma units are 1/USD; sigma2 units USD^2/s; k units 1/USD; tau seconds. Dimensional sanity: gamma*sigma2*tau is USD, gamma/k is dimensionless.
- Trust pytest's total counts over any inline arithmetic here; the requirement is ALL GREEN including the new tests.
