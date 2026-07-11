# Avellaneda-Stoikov Market Maker (Deribit BTC-PERPETUAL)

A live crypto market-making research system. It quotes two strategies side by side on real Deribit mainnet market data, an Avellaneda-Stoikov model and a fixed-spread baseline, and measures them with a deliberately conservative fill model, full inverse-perpetual accounting, funding costs, and a hard risk layer. Built to answer one question honestly: does the strategy have edge, and if not, exactly where does the money go?

**Status:** live multi-day measurement run in progress on a VPS. Research write-up (parameter sweep with walk-forward validation over the recorded data) follows when the run completes.

## Why this exists

Most hobby market-making projects report profits from a fill simulator that quietly grants fills the market would never have given. This project takes the opposite stance:

- **Fills are only granted when a real trade strictly crosses the quote.** Resting at the touch earns nothing in the simulation. This makes the measured edge a conservative floor, biased against the strategy, and it is treated as exactly that in all analysis.
- **Every cost is counted:** adverse selection is tracked per fill against the forward mid, funding is accrued from the live ticker at the exchange formula, and equity is marked in BTC using correct inverse-perpetual math.
- **Every risk action is recorded.** Inventory caps, kill-switch trips, and stale-quote pulls are written to an events table so the final numbers can be audited against what the risk layer actually did.

An early 29-hour run at a naive configuration (5 USD half-spread, 1 s requote) measured a statistically significant negative per-fill edge for both strategies under this floor model, while the A-S inventory control held position roughly 40x tighter than the baseline. That result is the motivation for the research phase: decompose the loss (spread capture vs adverse selection vs inventory vs funding), then tune on training days and report held-out test days only.

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

Design points:

- Single asyncio event loop; all timing decisions use exchange timestamps, never local clocks, so live and replay behave identically.
- Every raw websocket message is recorded to JSONL. The replay harness reruns any recorded session deterministically (verified byte-identical across runs), which turns the recorder into an offline research platform for parameter sweeps.
- Both strategies run in isolated lanes on the same feed with identical risk limits, so the baseline comparison is fair.
- Feed gaps longer than 10 s pull all resting sim quotes before the next event is processed, so quotes from before an exchange outage can never be filled at stale prices.

## Strategies

**Fixed-spread baseline:** symmetric quotes at mid plus/minus a constant half-spread. Exists so the A-S model has something honest to beat.

**Avellaneda-Stoikov:** reservation price `r = mid - q * gamma * sigma^2 * tau` and half-spread `delta = gamma * sigma^2 * tau / 2 + (1/gamma) * ln(1 + gamma/k)`, with a stationary constant-tau approximation and an EWMA volatility estimator fed from trade prices. The inventory term skews quotes to actively unload position, which is the behavior the measurement confirmed (position held ~40x tighter than baseline at the same spread).

## Risk layer

- **Inventory cap:** when position reaches the cap, the growing side is suppressed while the unloading side keeps quoting. Bind and release are recorded per episode.
- **Drawdown kill switch:** if equity drops a configured amount from its session peak, the lane stops quoting permanently. No synthetic flatten fill is injected, because that would pollute the fill methodology; the terminal open position is reported as-is.
- **Stale-quote pull:** described above, closes the exchange-outage hole.

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

`run_testnet_demo.py` proves order plumbing against the Deribit testnet: authenticate, rest a passive order, amend, cancel, take a fill, flatten. Credentials come from `DERIBIT_TESTNET_CLIENT_ID` / `DERIBIT_TESTNET_CLIENT_SECRET` environment variables. Testnet fills are not used in any reported measurement.

## Methodology notes (read before quoting any numbers)

1. The strict-cross fill rule excludes all at-touch fills a real queue position would earn. Measured edge is therefore a lower bound. The research phase brackets it with an at-touch upper bound.
2. Funding is accrued at the exchange formula from the live ticker; sessions before the funding module existed are labeled as such.
3. Exchange outages appear as gaps, not invented data. One 87-minute Deribit maintenance window occurred during an early run and is disclosed in the analysis rather than smoothed over.
4. All measured numbers come from real mainnet public market data. Nothing here is financial advice, and paper results do not include exchange fees or queue-position effects.

## Project history

Development followed written milestone plans (in `docs/superpowers/plans/`) with test-driven implementation, an adversarial audit pass that found and fixed real issues (an unbuilt risk layer, unmodeled funding, the stale-quote hole), and a restarted clean measurement run once both were fixed. The plans and the audit trail are left in the repo on purpose: the process is part of the point.
