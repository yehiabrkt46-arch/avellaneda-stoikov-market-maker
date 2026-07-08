# Milestone 4.5: Risk Layer + Funding Accrual

Date: 2026-07-09
Status: Approved (Fable plan; Sonnet implements; Fable reviews and verifies)

## Why

Fable audit (2026-07-08) found the spec's risk layer (spec section `risk/`) was never
built, and perp funding is unmodeled. Consequences observed in the verified 29h run:
baseline drifted to a -$13,440 position (134x quote size) and its +$187.68 "profit" is an
unhedged directional ride with no cost of carry. The live 7-day VPS run must be restarted
with both fixes so the measured comparison is clean.

## Deliverables

1. `mm_bot/risk.py`: per-lane risk manager (inventory cap, drawdown kill switch).
2. Engine wiring: risk filter applied to every quote decision; stale-data quote pull.
3. Funding accrual: live funding rate from the Deribit ticker channel, accrued per lane.
4. `events` table: every risk action recorded (for honest disclosure in the write-up).
5. Tests for all of the above (extend the existing 90-test suite; TDD where practical).
6. VPS redeploy + run restart (Fable does this after verification).

## Design

### 1. Risk manager (`mm_bot/risk.py`)

```python
class RiskManager:
    def __init__(self, cfg: StrategyConfig, on_event) -> None: ...
    def filter_quotes(self, q: QuotePair, position_usd: float,
                      equity_usd: float | None, ts_ms: int) -> QuotePair: ...
    @property
    def killed(self) -> bool: ...
```

- **Inventory cap** (`inventory_cap_usd`, new StrategyConfig field, default 500.0):
  if `position_usd >= cap`, suppress the bid (stop buying); if `position_usd <= -cap`,
  suppress the ask (stop selling). The unloading side stays quoted. Emit an `event`
  (kind=`cap_bind`, detail=side+position) once per bind episode, not per quote
  (track a `_cap_bound` flag; emit `cap_unbind` when it releases).
- **Drawdown kill switch** (`max_drawdown_usd`, new StrategyConfig field, default 100.0):
  track per-lane peak `equity_usd`. If `peak - equity > max_drawdown_usd`: permanently
  stop quoting for the session (both sides None forever), emit `event`
  kind=`kill_switch`. No synthetic flatten fill: the terminal open position and its
  mark-to-mid equity are reported as-is and disclosed (a synthetic mid fill would pollute
  the strict-cross fill methodology). This is a documented deviation from the spec's
  "flattens (in sim)" wording; rationale: fill-methodology purity.
- `QuotePair` already supports what we need if bid/ask can be None; check
  `mm_bot/strategy/base.py` and make bid/ask `float | None` if not already.
  `FillSimulator.set_quotes` already accepts None sides.

### 2. Stale-data quote pull (engine)

All timing uses exchange timestamps. In `PaperEngine.on_event`, before processing any
event: if `ts - last_event_ts > stale_quote_pull_s` (new StoreConfig field, default 10.0),
clear all lanes' sim quotes (set_quotes(None, None, 0)) and emit `event`
kind=`stale_pull` per lane, then process the event normally. This prevents quotes placed
before a feed gap (e.g. the observed 87-min exchange maintenance window) from being
"filled" by the first post-gap trade at a long-stale price. Track `last_event_ts` on the
engine from every book/trade event.

### 3. Funding accrual

- Feed: add `ticker.BTC-PERPETUAL.100ms` to the subscription list in the feed client.
  Parse into a new `Ticker` message (fields: `timestamp_ms`, `funding_8h`, `mark_price`).
  Recorder records raw ticker messages like everything else (replay compatibility).
- Engine: on each `Ticker` event, store `latest_funding_8h` and `latest_mark`.
- Accrual at rollup cadence (60s), per lane, exact formula:
  `funding_btc_delta = -position_usd / mark * funding_8h * (elapsed_s / 28800.0)`
  accumulated into `portfolio.funding_btc` (new Portfolio field, starts 0.0).
  Sign convention: positive funding_8h means longs pay shorts. A long (positive
  position_usd) with positive funding loses BTC. Verify sign in tests both ways.
- Equity: `equity_btc(mark) = btc_cash + funding_btc - position_usd / mark`.
  This changes existing equity semantics only when funding_btc != 0, so all existing
  tests stay green if they never accrue funding.
- Rollups: new column `funding_btc` (cumulative). Schema: add column via
  `ALTER TABLE rollups ADD COLUMN funding_btc REAL DEFAULT 0.0` guarded by a check in
  `Store.__init__` (append-only migration; existing DBs keep working).
- If no ticker has arrived yet (warmup), accrue nothing and count the skipped interval
  in a log line, never invent a rate.

### 4. Events table

`events(id INTEGER PK, session_id TEXT, ts_ms INTEGER, strategy TEXT, kind TEXT,
detail TEXT)`. Kinds: `cap_bind`, `cap_unbind`, `kill_switch`, `stale_pull`. Written via
`Store.record_event`. Created in `Store.__init__` like other tables.

### 5. Config

New `StrategyConfig` fields: `inventory_cap_usd: float = 500.0`,
`max_drawdown_usd: float = 100.0`. New `StoreConfig` field:
`stale_quote_pull_s: float = 10.0`. Update `config.yaml` to set them explicitly for both
lanes (same values both lanes; the baseline must face identical risk limits or the
comparison is unfair).

### 6. What does NOT change

- Fill rule (strict cross) untouched.
- Adverse tracker untouched.
- Existing table schemas untouched except the additive rollups column + new events table.
- Replay harness must still run old recordings (they simply have no ticker messages:
  funding stays 0, matching old behavior).

## Test plan (extend suite; all existing 90 must stay green)

- risk: cap binds long side / short side; unloading side stays; unbind re-quotes;
  kill switch trips on drawdown from peak (not from start); killed lane never quotes
  again; events emitted exactly once per episode.
- engine: stale gap pulls quotes before processing (a trade right after a >10s ts gap
  must NOT fill a pre-gap quote); normal cadence unaffected.
- funding: sign both directions; accrual proportional to elapsed time; no ticker -> no
  accrual; equity includes funding_btc; rollup column persisted.
- store: migration adds column to a pre-existing DB file without data loss; events
  round-trip.
- replay: old recording (no ticker) replays deterministically with funding_btc == 0.

## Rollout (Fable)

1. Review full diff, run suite, run replay determinism check on the existing recording.
2. Short local live smoke (5 min): confirm ticker flowing, funding accruing, caps quiet.
3. VPS: stop `mm-bot-paper`, deploy new tarball + .git, wipe `data/` on VPS (old session
   preserved locally), restart. New session row = the real 7-day measurement clock start.
