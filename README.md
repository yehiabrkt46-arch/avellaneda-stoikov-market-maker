# Avellaneda-Stoikov market maker (Deribit BTC-PERPETUAL)

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

A live market-making research system for the Deribit BTC inverse perpetual. It runs an Avellaneda-Stoikov quoting model next to a fixed-spread baseline on real mainnet market data and measures both with a fill model built to understate performance rather than flatter it. The goal is to answer one question: does the strategy have edge, and if not, where does the money go?

**Status:** the multi-day live measurement run is complete (9.66 days) and the parameter sweep with walk-forward validation over the recorded data is done. See Results and Parameter sweep below.

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

The Avellaneda-Stoikov lane quotes around a reservation price `r = mid - q * gamma * sigma^2 * tau` with half-spread `delta = gamma * sigma^2 * tau / 2 + (1/gamma) * ln(1 + gamma/k)`, using a stationary constant-tau approximation and an EWMA volatility estimator fed from trade prices. The inventory term skews quotes to unload position. An early 29-hour run with no risk layer measured position about 40x tighter than the baseline; once both lanes ran with an identical inventory cap (see Risk layer and Results below), the baseline's inventory was bounded too, so the gap over the full 9.66-day run narrowed to about 1.6x tighter on average, same worst case.

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

## Results

Measurement window: 2026-07-11 21:16:33 UTC to 2026-07-21 13:00:17 UTC, 9.66 days continuous, commit `9b58e16` the whole time. Three earlier sessions before this one (about 2.7 days, then two sessions of roughly 2.6 and 2.7 hours) crashed or were bounced during initial VPS setup and are excluded from the numbers below; nothing in the reported window depends on them. Inside the reported window there are two gaps, 10.2 and 5.5 minutes on 2026-07-15, both a stale-quote pull on a feed timestamp gap rather than a crash. All numbers below are recomputed independently from the raw recorded feed via the deterministic replay engine (`scripts/run_full_replay.py`), not read off the live rollups, so they double as a correctness check on the live run.

| | fixed_spread | avellaneda_stoikov |
|---|---|---|
| fills | 27,198 | 33,538 |
| average \|position_usd\| | 321.9 | 205.1 |
| worst-case \|position_usd\| | 590.0 | 590.0 |
| final equity_usd | -56.44 | -106.77 |
| equity_usd range | 0.33 to -58.34 | 0.21 to -107.2 |
| inventory cap binds | 1,633 | 405 |
| drawdown kill switch | never fired | fired once |

Both lanes share the same 500 USD inventory cap and 100 USD drawdown kill switch. The A-S lane's cap bound far less often (405 vs 1,633 times) and its average inventory ran about 1.6x tighter, so the inventory-control mechanism worked as designed. It also finished with a worse P&L: -106.77 vs -56.44.

The reason is fill count, not model failure. A-S requotes more actively around its reservation price, so it took 23% more fills than the baseline over the same window. Both strategies carry a negative per-fill trading edge under this project's deliberately conservative fill model (a real, previously measured cost: the strict-cross rule only grants fills a trade actually swept through, so it excludes all the benign at-touch fills a resting queue position would earn in reality, making the measured edge a floor rather than the strategies' true edge). More fills at a floor-negative edge means more realized loss, even with tighter inventory.

The kill switch entry in the table is not a footnote, it changed the outcome. It tripped once, at 2026-07-21 00:19:53 UTC, 90.3% of the way through the run, the instant cumulative equity crossed -100 USD from its peak. From that point the lane held its position at -280 USD with no further quoting, exactly as designed (no synthetic flatten), and drifted a further 7 USD to its final -106.77 over the remaining 12.7 hours from mark-to-market price movement and funding on the frozen position. So about 93% of the A-S lane's total loss had already happened before the kill switch fired, from the ordinary per-fill edge described above; the switch did its job of stopping further risk-taking once the configured threshold was reached, it did not cause the loss. The baseline's own drawdown from peak only reached about 58.7 USD, comfortably under the 100 USD threshold, which is why it never tripped.

Honest read for anyone hiring for this: the risk layer (inventory cap, drawdown kill switch, stale-quote pull) all performed correctly and are independently verifiable in the events table. The Avellaneda-Stoikov model delivers on inventory control. It has not been shown to be more profitable than a naive fixed-spread baseline at this configuration (5 USD half-spread, 1 s requote); both are negative under the conservative fill floor, and the more active strategy loses more in absolute terms. That is a config and edge-decomposition problem, not evidence the model is broken, which the parameter sweep below was built to check.

## Parameter sweep and walk-forward validation

The 9.66-day recording was replayed once per candidate configuration through the same deterministic engine (`scripts/run_param_sweep.py`), each candidate scored on per-fill spread capture plus adverse selection (funding excluded from this score; it is negligible, on the order of 1e-6 BTC over the whole run per the numbers above). Fixed_spread swept half_spread_usd over 3, 5, 8 USD. Avellaneda-Stoikov swept the two levers that actually change its behavior post-warmup, gamma over 0.0005, 0.001, 0.002 crossed with horizon_s over 30, 60, 120 seconds, 9 combinations. Twelve candidates total, each a full pass over the recorded feed.

Selection was walk-forward and chronological, not shuffled: the earliest 8 of the 11 distinct calendar days in the recording picked the winning config per strategy by total score, the remaining 3 days, never seen during selection, are reported as the held-out result.

| | winner | train score (8 days) | test score (3 days, held out) |
|---|---|---|---|
| fixed_spread | half_spread_usd = 8.0 | -23.57 USD | -2.86 USD |
| avellaneda_stoikov | gamma = 0.002, horizon_s = 120 | -68.66 USD | -19.42 USD |

Two honest caveats. First, both winners sit at the edge of the grid that was searched (the widest spread tried, and the highest gamma / longest horizon tried), so a wider spread or a more risk-averse gamma than what was tested might do better still; that direction was not explored given the roughly 4.5 hours of VPS time the 12-candidate grid already cost. Second, even after tuning both strategies independently on their own train days, fixed_spread's best configuration still beats Avellaneda-Stoikov's best configuration on both train and held-out test score. Retuning did not close the gap the naive-config run showed, it held up out of sample. Nothing here has been shown to be profitable in an absolute sense either, both winners are still net negative under the conservative fill floor; retuning reduced the loss, it did not reverse its sign.

## Project history

Development followed written milestone plans (kept in `docs/superpowers/plans/`), test-first where practical. A later audit pass found real problems: the risk layer required by the spec had never been built, funding was unmodeled, and quotes resting through a feed gap could be filled at long-stale prices. All three were fixed and the measurement run was restarted clean; that restarted run is the 9.66-day result reported above. The plans and the audit trail stay in the repo on purpose; the process is part of the point.
