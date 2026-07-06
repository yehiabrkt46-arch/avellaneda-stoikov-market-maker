# mm-bot: Live Avellaneda-Stoikov Market Maker, Design Spec

Date: 2026-07-06
Status: Approved by Yehia (design review in Claude Code session)

## Purpose

A live market-making bot implementing the Avellaneda-Stoikov optimal quoting model against
real Deribit BTC-PERPETUAL market data, producing honestly measured performance numbers
(P&L, fill ratio, inventory variance, adverse selection) suitable for a quant CV targeting
market-maker and prop-trading roles. Every reported number must be defensible under
interview questioning: measured from a live run, with a baseline comparison, stated time
period, and stated sample size.

## Locked decisions

- Instrument: Deribit BTC-PERPETUAL (single instrument).
- Language/stack: Python 3.12, asyncio, single event loop, no threads.
- Execution architecture: hybrid.
  - All measured numbers come from real Deribit mainnet public market data (L2 book plus
    trade stream, unauthenticated, free) with fills simulated locally against real trade
    flow.
  - A separate thin Deribit testnet connector proves real order placement, cancel/amend,
    and rate-limit plumbing in a short supervised demo. Testnet fill numbers are never
    reported: testnet books are not real markets.
- Deployment: existing VPS (already runs CSAlpha site), managed by pm2, for the multi-day
  measurement run.
- Storage: SQLite, append-only tables.
- Model orchestration for the build: Fable 5 plans and does final verification; Sonnet 5
  subagents write code, tests, and do grunt work; Fable reviews every diff.
- Style: no em dashes or double hyphens in README or any prose that may go public.

## Architecture

Event-driven asyncio application. One process, one event loop.

### Components

feed/
- Deribit mainnet websocket client (wss://www.deribit.com/ws/api/v2).
- Subscriptions: book.BTC-PERPETUAL.100ms (L2 depth deltas) and
  trades.BTC-PERPETUAL.100ms (every public trade).
- Maintains a local L2 order book from deltas. Detects sequence gaps via the
  change_id/prev_change_id chain; on gap, discards book and refetches a full snapshot.
- Reconnect on drop with exponential backoff plus jitter. Handles Deribit
  heartbeat/test_request messages.
- Raw message recorder (append-only JSONL or SQLite) for replay testing.

sim/
- Fill simulator. Strategy registers desired bid/ask quotes; simulator watches the real
  trade stream.
- Fill rule (conservative, the honesty anchor for every reported number): a quote fills
  only when a real printed trade strictly crosses through it, meaning trade price below
  the bot's bid fills the bid, trade price above the bot's ask fills the ask. No fills
  at-touch, no queue-position modeling. Partial fills sized by the printed trade quantity.
- This rule understates fill rate relative to a real queue position. That bias direction
  (conservative) is documented in the write-up.

strategy/
- Common interface: on_tick(book_state, inventory, clock) -> desired (bid, ask) or None.
- FixedSpread: symmetric quotes at a constant configured spread around mid. The baseline.
- AvellanedaStoikov:
  - Reservation price: r = s - q * gamma * sigma^2 * (T - t), where s is mid, q is
    inventory, gamma is risk aversion, sigma is volatility, (T - t) is time to horizon.
  - Optimal half-spread: delta = gamma * sigma^2 * (T - t) / 2 + (1 / gamma) * ln(1 + gamma / k).
  - sigma: EWMA of mid-price returns, estimated online.
  - k: order-flow intensity parameter, calibrated from observed trade arrival rate as a
    function of distance from mid (exponential decay fit), recalibrated on a rolling
    window.
  - Horizon handling: rolling/restarting horizon (session-based T), documented in code.

risk/
- Hard inventory cap: quotes on the loaded side are pulled when |q| exceeds the cap.
- Drawdown kill switch: bot stops quoting and flattens (in sim) past a configured max
  drawdown.
- Stale-data watchdog: if no market data message arrives for N seconds, all quotes are
  pulled until the feed recovers.

metrics/
- Realized P&L and mark-to-mid unrealized P&L.
- Fill count, quote count, quote-to-fill ratio.
- Inventory time series and inventory variance.
- Adverse selection per fill: mid(t_fill + K seconds) minus fill price, signed by side.
- Periodic rollups persisted to storage.

store/
- SQLite, append-only: quotes placed, fills, sampled book snapshots, metric rollups,
  session metadata (config hash, git commit, start/stop times).
- Crash-safe: writes are append-only inserts, WAL mode.

exec_testnet/
- Thin Deribit testnet (test.deribit.com) connector: authentication, place/cancel/amend
  orders, rate-limit error handling, partial-fill tracking.
- Used for one short supervised demo run to prove the plumbing works against a real
  exchange API. Its numbers are never reported as performance.

runner
- Config via YAML file (instrument, strategy params, risk limits, storage paths).
- Structured logging (JSON lines).
- pm2 process file for VPS deployment.

### Data flow

WS message -> feed updates local book -> strategy tick (event-driven, throttled to about
1 per second) -> desired bid/ask -> sim registers quotes -> real trade prints -> sim emits
fill events -> inventory and P&L update -> metrics -> SQLite.

## Error handling

- Websocket reconnect with exponential backoff plus jitter.
- Sequence gap: discard book, refetch snapshot, resubscribe.
- Stale-data watchdog pulls quotes rather than quoting on a dead book.
- All event timestamps taken from exchange messages, not the local clock.
- pm2 restarts the process on crash; append-only storage means a crash loses at most the
  in-flight event.
- Every restart is recorded in session metadata so the measurement write-up can disclose
  uptime and gaps honestly.

## Testing

- pytest throughout.
- Unit: book delta application including gap and snapshot cases; fill-simulator rules
  (property-based: filled quantity never exceeds printed quantity, bids never fill on
  trades above them, asks never fill on trades below them, quantity conservation);
  Avellaneda-Stoikov formulas checked against hand-computed values; EWMA volatility and
  k-calibration against synthetic data with known parameters.
- Integration: deterministic replay of recorded real market data through the full
  feed -> strategy -> sim -> metrics pipeline.
- Process: Sonnet 5 subagents write code and tests; Fable reviews every diff and performs
  final verification, including live smoke runs.

## Measurement plan (the CV payload)

- Run FixedSpread baseline and AvellanedaStoikov simultaneously on the same live data
  feed for at least 7 continuous days on the VPS.
- Report for both strategies: realized P&L, mark-to-mid P&L, inventory variance,
  quote-to-fill ratio, adverse selection cost, number of fills, exact date range, and
  disclosed downtime.
- Nothing invented, nothing backtest-only, no number without documented methodology.
  The baseline comparison is mandatory (lesson from the two volatility projects dropped
  from the CV for undefendable numbers).

## Build order (milestones)

1. Feed: websocket client, local book maintenance, raw recorder. Verify with an overnight
   run: no gaps unhandled, book checksums consistent, clean reconnects.
2. Sim plus baseline: fill simulator, FixedSpread strategy, metrics, SQLite storage.
3. Model: AvellanedaStoikov strategy, EWMA volatility estimator, k calibration.
4. Hardening and run: risk layer, VPS deployment via pm2, 7-day dual-strategy run.
5. Testnet connector demo.
6. Write-up: README documenting model, fill-simulation methodology and its conservative
   bias, and the measured numbers only.

## Out of scope (YAGNI)

- Multiple instruments or exchanges.
- Live dashboard or web UI (logs plus SQLite queries suffice; revisit after milestone 4).
- Real-money trading.
- Queue-position fill modeling (documented as future work).
- GLFT extension (possible follow-up after base model runs).
