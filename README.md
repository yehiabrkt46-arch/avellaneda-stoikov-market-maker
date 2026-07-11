# Avellaneda-Stoikov market maker (Deribit BTC-PERPETUAL)

A live market-making research system for the Deribit BTC inverse perpetual. It runs an Avellaneda-Stoikov quoting model next to a fixed-spread baseline on real mainnet market data and measures both with a fill model built to understate performance rather than flatter it. The goal is to answer one question: does the strategy have edge, and if not, where does the money go?

**Status:** a multi-day live measurement run is in progress on a VPS. A research write-up (parameter sweep with walk-forward validation over the recorded data) follows when it completes.

## Why this exists

Most hobby market-making projects report profits from a fill simulator that quietly grants fills the market would never have given. This project takes the opposite approach.

The simulator grants a fill only when a real trade strictly crosses the quote. Resting at the touch earns nothing, so the measured edge is a floor, biased against the strategy, and the analysis treats it as exactly that.

The engine also counts every cost: it tracks adverse selection per fill against the forward mid, accrues funding from the live ticker at the exchange formula, and marks equity in BTC with correct inverse-perpetual math. Risk actions leave a paper trail too. Inventory cap binds, kill-switch trips, and stale-quote pulls all land in an events table, so the final numbers can be checked against what the risk layer did.

An early 29-hour run at a naive configuration (5 USD half-spread, 1 s requote) measured a statistically significant negative per-fill edge for both strategies under this floor model, while the A-S inventory control held position about 40x tighter than the baseline. That result motivates the research phase: decompose the loss into spread capture, adverse selection, inventory, and funding, then tune on training days and report held-out test days only.

## Architecture

```
mm_bot/
  feed/       Deribit websocket client: L2 book, trades, ticker, heartbeats,
              reconnect with gap-triggered resubscribe; raw JSONL recorder
  strategy/   FixedSpread baseline and Avellaneda-Stoikov quoting model
              (reservation price + optimal spread, EWMA volatility estimator)
  paper/      Paper-trading engine: strict-cross fill simulator, inverse-perp
              portfolio accounting, funding accrual, adverse-selection tracker,
              deterministic replay of recorded sessions
  risk.py     Per-strategy risk manager: inventory cap, drawdown kill switch
  store/      Append-only SQLite (WAL): quotes, fills, rollups, risk events
  exec_testnet/  Deribit testnet execution connector (auth, place/amend/cancel)
```

A few design decisions worth knowing about:

- Single asyncio event loop. All timing decisions use exchange timestamps, never local clocks, so live and replay behave identically.
- Every raw websocket message is recorded to JSONL. The replay harness reruns any recorded session deterministically (verified byte-identical across runs), which turns the recorder into an offline research platform for parameter sweeps.
- Both strategies run in isolated lanes on the same feed with identical risk limits, so the baseline comparison is fair.
- Feed gaps longer than 10 seconds pull all resting sim quotes before the next event is processed. Quotes placed before an exchange outage can never fill at stale prices afterward.

## Strategies

The fixed-spread baseline quotes symmetrically at mid plus and minus a constant half-spread. It is there so the A-S model has a benchmark to beat.

The Avellaneda-Stoikov lane quotes around a reservation price `r = mid - q * gamma * sigma^2 * tau` with half-spread `delta = gamma * sigma^2 * tau / 2 + (1/gamma) * ln(1 + gamma/k)`, using a stationary constant-tau approximation and an EWMA volatility estimator fed from trade prices. The inventory term skews quotes to unload position, and the measurement confirmed that behavior: position stayed about 40x tighter than the baseline at the same spread.

## Risk layer

Each strategy lane has its own risk manager. When position reaches the inventory cap, the growing side stops quoting while the unloading side keeps working; every bind and release is recorded as an event. If equity drops a configured amount from its session peak, a kill switch stops the lane permanently. No synthetic flatten fill is injected, since that would pollute the fill methodology, so the terminal open position is reported as-is and disclosed.

## Running it

Requires Python 3.12+.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q        # 118 tests

# record market data + paper-trade both strategies (config.yaml)
.venv/bin/python run_paper.py

# feed recorder only
.venv/bin/python run_recorder.py
```

Deployment (pm2 on a VPS) is documented in `deploy/DEPLOY.md`.

### Testnet execution connector

`run_testnet_demo.py` proves order plumbing against the Deribit testnet: authenticate, rest a passive order, amend, cancel, take a fill, flatten. Credentials come from the `DERIBIT_TESTNET_CLIENT_ID` and `DERIBIT_TESTNET_CLIENT_SECRET` environment variables. Testnet fills are not used in any reported measurement.

## Methodology notes (read before quoting any numbers)

1. The strict-cross fill rule excludes all at-touch fills a real queue position would earn. Measured edge is therefore a lower bound. The research phase brackets it with an at-touch upper bound.
2. Funding is accrued at the exchange formula from the live ticker. Sessions recorded before the funding module existed are labeled as such.
3. Exchange outages appear as gaps, not invented data. One 87-minute Deribit maintenance window occurred during an early run and is disclosed in the analysis rather than smoothed over.
4. All measured numbers come from real mainnet public market data. Paper results do not include exchange fees or queue-position effects, and none of this is financial advice.

## Project history

Development followed written milestone plans (kept in `docs/superpowers/plans/`), test-first where practical. A later audit pass found real problems: the risk layer required by the spec had never been built, funding was unmodeled, and quotes resting through a feed gap could be filled at long-stale prices. All three were fixed and the measurement run was restarted clean. The plans and the audit trail stay in the repo on purpose; the process is part of the point.
