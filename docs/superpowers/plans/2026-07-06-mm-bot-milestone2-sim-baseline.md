# mm-bot Milestone 2: Fill Sim, FixedSpread Baseline, Metrics, SQLite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paper-trading engine: strategies quote around the live book, a conservative fill simulator fills quotes against real printed trades, an inverse-perp portfolio tracks P&L in BTC, adverse selection is measured per fill, and everything persists to SQLite.

**Architecture:** `PaperEngine` receives feed events (from `DeribitFeedClient` or a replay). Each strategy runs in its own `StrategyLane` (strategy + `FillSimulator` + `Portfolio` + adverse-selection tracker), so milestone 3 adds Avellaneda-Stoikov as a second lane beside the baseline on the same feed. All timestamps come from exchange messages. Storage is append-only SQLite in WAL mode.

**Tech Stack:** Python 3.12+, stdlib `sqlite3`, existing mm_bot.feed modules, pytest.

**Spec:** `docs/superpowers/specs/2026-07-06-mm-bot-design.md`. Prerequisite: milestone 1 (branch `milestone-1-feed`) complete, 23 tests green.

**Domain facts the engineer needs:**
- Deribit BTC-PERPETUAL is an INVERSE perpetual: contracts are 10 USD each, P&L settles in BTC. Long N contracts entered at p1, exited at p2 earns `10*N*(1/p1 - 1/p2)` BTC (positive when p2 > p1). Trade `amount` fields are in USD.
- Bookkeeping that reproduces this for any fill sequence: a BUY of `usd` notional at price p does `position_usd += usd; btc_cash += usd/p`. A SELL does the reverse. Then `equity_btc(mark) = btc_cash - position_usd/mark`. Verify: buy 100 at 50000 then mark 100000: equity = 0.002 - 0.001 = +0.001 BTC = 10*10*(1/50000 - 1/100000). Flat position: equity is realized P&L.
- Tick size for BTC-PERPETUAL is 0.5 USD. Bids round DOWN to tick, asks round UP (never quote tighter than intended).
- Conservative fill rule (the honesty anchor, from the spec): a resting quote fills only when a real printed trade STRICTLY crosses it: trade.price < our bid fills the bid; trade.price > our ask fills the ask. Fill price is OUR quote price (we are the maker). Fill quantity is capped by the printed trade amount and by the quote's remaining size. No fills at-touch, no queue modeling.
- Adverse selection per fill: mid moved against us K seconds (default 5) after the fill. For a buy fill: `mid_at_fill - mid_after` (positive = we bought right before price dropped = adverse). For a sell fill: `mid_after - mid_at_fill`.

---

### Task 1: Config extensions

**Files:**
- Modify: `mm_bot/config.py`
- Modify: `tests/test_config.py`
- Modify: `config.yaml`

- [ ] **Step 1: Add failing tests** (append to `tests/test_config.py`)

```python
def test_load_config_strategy_and_store_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("")
    cfg = load_config(p)
    assert len(cfg.strategies) == 1
    s = cfg.strategies[0]
    assert s.kind == "fixed_spread"
    assert s.name == "fixed_spread"
    assert s.half_spread_usd == 5.0
    assert s.quote_size_usd == 100.0
    assert s.tick_size == 0.5
    assert s.requote_interval_s == 1.0
    assert cfg.store.db_path == "data/mm.sqlite"
    assert cfg.store.rollup_interval_s == 60
    assert cfg.store.adverse_horizon_s == 5.0


def test_load_config_multiple_strategies(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "strategies:\n"
        "  - name: base\n    half_spread_usd: 4.0\n"
        "  - name: wide\n    half_spread_usd: 12.0\n"
        "store:\n  db_path: other.sqlite\n"
    )
    cfg = load_config(p)
    assert [s.name for s in cfg.strategies] == ["base", "wide"]
    assert cfg.strategies[1].half_spread_usd == 12.0
    assert cfg.store.db_path == "other.sqlite"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_config.py -v`
Expected: 2 new tests FAIL (`AttributeError` or `TypeError` for strategies/store), 2 old pass.

- [ ] **Step 3: Implement** (add to `mm_bot/config.py`, keep existing classes unchanged)

```python
@dataclass(frozen=True)
class StrategyConfig:
    kind: str = "fixed_spread"
    name: str = "fixed_spread"
    half_spread_usd: float = 5.0
    quote_size_usd: float = 100.0
    tick_size: float = 0.5
    requote_interval_s: float = 1.0


@dataclass(frozen=True)
class StoreConfig:
    db_path: str = "data/mm.sqlite"
    rollup_interval_s: int = 60
    adverse_horizon_s: float = 5.0
```

Change `Config` to:

```python
@dataclass(frozen=True)
class Config:
    feed: FeedConfig
    recorder: RecorderConfig
    strategies: tuple[StrategyConfig, ...]
    store: StoreConfig
```

Change `load_config` to:

```python
def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return Config(
        feed=FeedConfig(**raw.get("feed", {})),
        recorder=RecorderConfig(**raw.get("recorder", {})),
        strategies=tuple(
            StrategyConfig(**s) for s in raw.get("strategies", [{}])
        ),
        store=StoreConfig(**raw.get("store", {})),
    )
```

Update `config.yaml` to:

```yaml
feed:
  instrument: BTC-PERPETUAL
recorder:
  data_dir: data
strategies:
  - kind: fixed_spread
    name: fixed_spread
    half_spread_usd: 5.0
    quote_size_usd: 100.0
store:
  db_path: data/mm.sqlite
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_config.py -v`
Expected: 4 passed. Full suite: 25 passed.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/config.py tests/test_config.py config.yaml
git commit -m "feat: strategy and store config sections"
```

---

### Task 2: Portfolio (inverse-perp P&L)

**Files:**
- Create: `mm_bot/paper/__init__.py` (empty)
- Create: `mm_bot/paper/portfolio.py`
- Test: `tests/test_portfolio.py`
- Modify: `pyproject.toml` (add `"mm_bot.paper"` to `[tool.setuptools] packages` list; also add `"mm_bot.strategy"` and `"mm_bot.store"` now for Tasks 4 and 5)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_portfolio.py
import pytest

from mm_bot.paper.portfolio import Fill, Portfolio


def fill(side, price, amount_usd, ts=1751800000000, trade_id="t1"):
    return Fill(timestamp_ms=ts, side=side, price=price, amount_usd=amount_usd, trade_id=trade_id)


def test_flat_portfolio_has_zero_equity():
    p = Portfolio()
    assert p.position_usd == 0.0
    assert p.equity_btc(60000.0) == 0.0


def test_buy_then_mark_up_matches_deribit_formula():
    p = Portfolio()
    p.apply_fill(fill("buy", 50000.0, 100.0))
    # long 100 USD (10 contracts), entry 50000, mark 100000
    # pnl_btc = 100 * (1/50000 - 1/100000) = 0.001
    assert p.equity_btc(100000.0) == pytest.approx(0.001)
    assert p.equity_usd(100000.0) == pytest.approx(100.0)


def test_round_trip_realizes_pnl_and_flattens():
    p = Portfolio()
    p.apply_fill(fill("buy", 50000.0, 100.0))
    p.apply_fill(fill("sell", 60000.0, 100.0))
    assert p.position_usd == pytest.approx(0.0)
    expected = 100.0 * (1 / 50000.0 - 1 / 60000.0)
    # flat position: equity independent of mark
    assert p.equity_btc(10.0) == pytest.approx(expected)
    assert p.equity_btc(1e9) == pytest.approx(expected)


def test_short_profits_when_price_falls():
    p = Portfolio()
    p.apply_fill(fill("sell", 60000.0, 100.0))
    assert p.position_usd == pytest.approx(-100.0)
    assert p.equity_btc(50000.0) == pytest.approx(100.0 * (1 / 50000.0 - 1 / 60000.0))
    assert p.equity_btc(50000.0) > 0


def test_long_loses_when_price_falls():
    p = Portfolio()
    p.apply_fill(fill("buy", 60000.0, 100.0))
    assert p.equity_btc(50000.0) < 0


def test_fill_count_increments():
    p = Portfolio()
    p.apply_fill(fill("buy", 50000.0, 10.0))
    p.apply_fill(fill("sell", 50000.0, 10.0))
    assert p.fill_count == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_portfolio.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.paper'`

- [ ] **Step 3: Implement**

```python
# mm_bot/paper/portfolio.py
"""Inverse-perpetual position and P&L accounting (Deribit BTC-PERPETUAL).

Contracts are USD-denominated, P&L settles in BTC. Long N contracts (10 USD
each) entered at p1 and exited at p2 earns 10*N*(1/p1 - 1/p2) BTC. The
bookkeeping below reproduces that for any fill sequence:
a buy of `usd` notional at price p adds +usd to position_usd and +usd/p to
btc_cash; equity_btc(mark) = btc_cash - position_usd/mark.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Fill:
    timestamp_ms: int
    side: str  # "buy" or "sell" (our side of the fill)
    price: float
    amount_usd: float
    trade_id: str


class Portfolio:
    def __init__(self) -> None:
        self.position_usd = 0.0
        self.btc_cash = 0.0
        self.fill_count = 0

    def apply_fill(self, fill: Fill) -> None:
        sign = 1.0 if fill.side == "buy" else -1.0
        self.position_usd += sign * fill.amount_usd
        self.btc_cash += sign * fill.amount_usd / fill.price
        self.fill_count += 1

    def equity_btc(self, mark: float) -> float:
        return self.btc_cash - self.position_usd / mark

    def equity_usd(self, mark: float) -> float:
        return self.equity_btc(mark) * mark
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_portfolio.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/paper tests/test_portfolio.py pyproject.toml
git commit -m "feat: inverse-perp portfolio accounting (BTC-settled P&L)"
```

---

### Task 3: Fill simulator

**Files:**
- Create: `mm_bot/paper/sim.py`
- Test: `tests/test_sim.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sim.py
from mm_bot.feed.messages import Trade
from mm_bot.paper.portfolio import Fill
from mm_bot.paper.sim import FillSimulator


def trade(price, amount, ts=1751800000150, trade_id="t1", direction="sell"):
    return Trade(
        instrument="BTC-PERPETUAL",
        trade_id=trade_id,
        trade_seq=1,
        timestamp_ms=ts,
        price=price,
        amount=amount,
        direction=direction,
    )


def make_sim():
    fills = []
    sim = FillSimulator(fills.append)
    return sim, fills


def test_no_quotes_no_fills():
    sim, fills = make_sim()
    sim.on_trade(trade(59000.0, 100.0))
    assert fills == []


def test_trade_through_bid_fills_buy_at_quote_price():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, 60010.0, 100.0)
    sim.on_trade(trade(59999.5, 50.0))
    assert len(fills) == 1
    f = fills[0]
    assert f.side == "buy"
    assert f.price == 60000.0  # our quote price, not the trade print
    assert f.amount_usd == 50.0


def test_trade_at_bid_does_not_fill():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, 60010.0, 100.0)
    sim.on_trade(trade(60000.0, 50.0))  # at-touch, conservative rule: no fill
    assert fills == []


def test_trade_through_ask_fills_sell():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, 60010.0, 100.0)
    sim.on_trade(trade(60010.5, 30.0, direction="buy"))
    assert len(fills) == 1
    assert fills[0].side == "sell"
    assert fills[0].price == 60010.0
    assert fills[0].amount_usd == 30.0


def test_partial_fills_deplete_quote_and_conserve_quantity():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, None, 100.0)
    sim.on_trade(trade(59999.0, 60.0, trade_id="a"))
    sim.on_trade(trade(59998.0, 60.0, trade_id="b"))
    sim.on_trade(trade(59997.0, 60.0, trade_id="c"))  # quote exhausted, no fill
    assert [f.amount_usd for f in fills] == [60.0, 40.0]
    assert sum(f.amount_usd for f in fills) == 100.0  # never exceeds quote size
    assert fills[0].trade_id == "a" and fills[1].trade_id == "b"


def test_fill_never_exceeds_printed_trade_amount():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, None, 1000.0)
    sim.on_trade(trade(59999.0, 10.0))
    assert fills[0].amount_usd == 10.0


def test_set_quotes_replaces_and_resets_remaining():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, None, 100.0)
    sim.on_trade(trade(59999.0, 100.0))  # fully fill
    sim.on_trade(trade(59999.0, 100.0))  # nothing left
    assert len(fills) == 1
    sim.set_quotes(60000.0, None, 100.0)  # re-quote restores size
    sim.on_trade(trade(59999.0, 100.0))
    assert len(fills) == 2


def test_none_side_is_not_quoted():
    sim, fills = make_sim()
    sim.set_quotes(None, 60010.0, 100.0)
    sim.on_trade(trade(59000.0, 100.0))  # would cross a bid if one existed
    assert fills == []


def test_uses_exchange_timestamp():
    sim, fills = make_sim()
    sim.set_quotes(60000.0, None, 100.0)
    sim.on_trade(trade(59999.0, 10.0, ts=1751800099999))
    assert fills[0].timestamp_ms == 1751800099999
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_sim.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.paper.sim'`

- [ ] **Step 3: Implement**

```python
# mm_bot/paper/sim.py
"""Conservative fill simulator.

A resting quote fills only when a real printed trade STRICTLY crosses it:
trade price below our bid fills the bid, trade price above our ask fills the
ask. Fill price is our quote price (maker). Fill size is capped by both the
printed trade amount and the quote's remaining size. No at-touch fills, no
queue-position modeling; this understates fill rate, which is the documented
conservative bias of every reported number.
"""
from mm_bot.feed.messages import Trade
from mm_bot.paper.portfolio import Fill


class FillSimulator:
    def __init__(self, on_fill) -> None:
        self._on_fill = on_fill  # sync callable(Fill)
        self._bid: tuple[float, float] | None = None  # (price, remaining_usd)
        self._ask: tuple[float, float] | None = None

    def set_quotes(
        self, bid_price: float | None, ask_price: float | None, size_usd: float
    ) -> None:
        self._bid = (bid_price, size_usd) if bid_price is not None else None
        self._ask = (ask_price, size_usd) if ask_price is not None else None

    @property
    def bid(self) -> tuple[float, float] | None:
        return self._bid

    @property
    def ask(self) -> tuple[float, float] | None:
        return self._ask

    def on_trade(self, trade: Trade) -> None:
        if self._bid is not None and trade.price < self._bid[0]:
            price, remaining = self._bid
            qty = min(remaining, trade.amount)
            remaining -= qty
            self._bid = (price, remaining) if remaining > 0 else None
            self._on_fill(
                Fill(
                    timestamp_ms=trade.timestamp_ms,
                    side="buy",
                    price=price,
                    amount_usd=qty,
                    trade_id=trade.trade_id,
                )
            )
        if self._ask is not None and trade.price > self._ask[0]:
            price, remaining = self._ask
            qty = min(remaining, trade.amount)
            remaining -= qty
            self._ask = (price, remaining) if remaining > 0 else None
            self._on_fill(
                Fill(
                    timestamp_ms=trade.timestamp_ms,
                    side="sell",
                    price=price,
                    amount_usd=qty,
                    trade_id=trade.trade_id,
                )
            )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_sim.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/paper/sim.py tests/test_sim.py
git commit -m "feat: conservative strict-cross fill simulator"
```

---

### Task 4: FixedSpread strategy

**Files:**
- Create: `mm_bot/strategy/__init__.py` (empty)
- Create: `mm_bot/strategy/base.py`
- Create: `mm_bot/strategy/fixed_spread.py`
- Test: `tests/test_fixed_spread.py`

(`pyproject.toml` already lists `mm_bot.strategy` from Task 2.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fixed_spread.py
from mm_bot.config import StrategyConfig
from mm_bot.strategy.base import QuotePair, round_to_tick
from mm_bot.strategy.fixed_spread import FixedSpreadStrategy


def test_round_to_tick():
    assert round_to_tick(60001.3, 0.5, down=True) == 60001.0
    assert round_to_tick(60001.3, 0.5, down=False) == 60001.5
    assert round_to_tick(60001.5, 0.5, down=True) == 60001.5
    assert round_to_tick(60001.5, 0.5, down=False) == 60001.5


def test_symmetric_quotes_around_mid():
    cfg = StrategyConfig(half_spread_usd=5.0, tick_size=0.5)
    strat = FixedSpreadStrategy(cfg)
    q = strat.quotes(mid=60000.25, position_usd=0.0, now_ms=0)
    assert isinstance(q, QuotePair)
    assert q.bid == 59995.0  # 59995.25 floored to tick
    assert q.ask == 60005.5  # 60005.25 ceiled to tick
    assert q.bid < 60000.25 < q.ask


def test_quotes_ignore_inventory_and_time():
    cfg = StrategyConfig(half_spread_usd=5.0, tick_size=0.5)
    strat = FixedSpreadStrategy(cfg)
    a = strat.quotes(mid=60000.0, position_usd=0.0, now_ms=0)
    b = strat.quotes(mid=60000.0, position_usd=-5000.0, now_ms=999999)
    assert a == b


def test_strategy_exposes_name():
    cfg = StrategyConfig(name="baseline")
    assert FixedSpreadStrategy(cfg).name == "baseline"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_fixed_spread.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.strategy'`

- [ ] **Step 3: Implement**

```python
# mm_bot/strategy/base.py
"""Strategy interface: mid + inventory + exchange time in, desired quotes out."""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class QuotePair:
    bid: float | None
    ask: float | None


def round_to_tick(price: float, tick: float, down: bool) -> float:
    n = price / tick
    rounded = math.floor(n) if down else math.ceil(n)
    return rounded * tick
```

```python
# mm_bot/strategy/fixed_spread.py
"""Baseline: symmetric quotes at a fixed half-spread around mid."""
from mm_bot.config import StrategyConfig
from mm_bot.strategy.base import QuotePair, round_to_tick


class FixedSpreadStrategy:
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_fixed_spread.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/strategy tests/test_fixed_spread.py
git commit -m "feat: FixedSpread baseline strategy with tick rounding"
```

---

### Task 5: SQLite store

**Files:**
- Create: `mm_bot/store/__init__.py` (empty)
- Create: `mm_bot/store/db.py`
- Test: `tests/test_store.py`

(`pyproject.toml` already lists `mm_bot.store` from Task 2.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store.py
import sqlite3

from mm_bot.store.db import Store


def test_creates_schema_and_session(tmp_path):
    db = tmp_path / "mm.sqlite"
    store = Store(db)
    store.start_session("s1", 1751800000000, "abc123", '{"x":1}')
    store.close()
    con = sqlite3.connect(db)
    row = con.execute("SELECT session_id, started_ts_ms, git_commit FROM sessions").fetchone()
    assert row == ("s1", 1751800000000, "abc123")
    for table in ("quotes", "fills", "rollups"):
        con.execute(f"SELECT * FROM {table}")  # table exists
    con.close()


def test_record_quote_and_rollup(tmp_path):
    store = Store(tmp_path / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    store.record_quote("s1", 1751800000000, "fixed_spread", 59995.0, 60005.0, 100.0)
    store.record_rollup(
        "s1", 1751800060000, "fixed_spread",
        position_usd=100.0, btc_cash=0.002, equity_btc=0.0001,
        equity_usd=6.0, mid=60000.0, fill_count=3, quote_count=60,
    )
    con = store.connection
    assert con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
    r = con.execute(
        "SELECT position_usd, equity_usd, fill_count FROM rollups"
    ).fetchone()
    assert r == (100.0, 6.0, 3)
    store.close()


def test_record_fill_and_update_adverse(tmp_path):
    store = Store(tmp_path / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    fill_id = store.record_fill(
        "s1", 1751800000000, "fixed_spread",
        side="buy", price=59995.0, amount_usd=50.0,
        trade_id="t9", mid_at_fill=60000.0,
    )
    con = store.connection
    assert con.execute("SELECT adverse_move_usd FROM fills WHERE id=?", (fill_id,)).fetchone()[0] is None
    store.set_adverse(fill_id, -1.5)
    assert con.execute("SELECT adverse_move_usd FROM fills WHERE id=?", (fill_id,)).fetchone()[0] == -1.5
    store.close()


def test_creates_parent_dir(tmp_path):
    store = Store(tmp_path / "nested" / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    store.close()
    assert (tmp_path / "nested" / "mm.sqlite").exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.store'`

- [ ] **Step 3: Implement**

```python
# mm_bot/store/db.py
"""Append-only SQLite persistence (WAL mode) for quotes, fills, rollups."""
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_ts_ms INTEGER NOT NULL,
    git_commit TEXT NOT NULL,
    config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    bid REAL,
    ask REAL,
    size_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    amount_usd REAL NOT NULL,
    trade_id TEXT NOT NULL,
    mid_at_fill REAL NOT NULL,
    adverse_move_usd REAL
);
CREATE TABLE IF NOT EXISTS rollups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    position_usd REAL NOT NULL,
    btc_cash REAL NOT NULL,
    equity_btc REAL NOT NULL,
    equity_usd REAL NOT NULL,
    mid REAL NOT NULL,
    fill_count INTEGER NOT NULL,
    quote_count INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    def start_session(
        self, session_id: str, started_ts_ms: int, git_commit: str, config_json: str
    ) -> None:
        self.connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (session_id, started_ts_ms, git_commit, config_json),
        )
        self.connection.commit()

    def record_quote(
        self, session_id: str, ts_ms: int, strategy: str,
        bid: float | None, ask: float | None, size_usd: float,
    ) -> None:
        self.connection.execute(
            "INSERT INTO quotes (session_id, ts_ms, strategy, bid, ask, size_usd)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, ts_ms, strategy, bid, ask, size_usd),
        )
        self.connection.commit()

    def record_fill(
        self, session_id: str, ts_ms: int, strategy: str, *,
        side: str, price: float, amount_usd: float, trade_id: str, mid_at_fill: float,
    ) -> int:
        cur = self.connection.execute(
            "INSERT INTO fills (session_id, ts_ms, strategy, side, price,"
            " amount_usd, trade_id, mid_at_fill) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, ts_ms, strategy, side, price, amount_usd, trade_id, mid_at_fill),
        )
        self.connection.commit()
        return cur.lastrowid

    def set_adverse(self, fill_id: int, adverse_move_usd: float) -> None:
        self.connection.execute(
            "UPDATE fills SET adverse_move_usd = ? WHERE id = ?",
            (adverse_move_usd, fill_id),
        )
        self.connection.commit()

    def record_rollup(
        self, session_id: str, ts_ms: int, strategy: str, *,
        position_usd: float, btc_cash: float, equity_btc: float,
        equity_usd: float, mid: float, fill_count: int, quote_count: int,
    ) -> None:
        self.connection.execute(
            "INSERT INTO rollups (session_id, ts_ms, strategy, position_usd,"
            " btc_cash, equity_btc, equity_usd, mid, fill_count, quote_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, ts_ms, strategy, position_usd, btc_cash, equity_btc,
             equity_usd, mid, fill_count, quote_count),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_store.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/store tests/test_store.py
git commit -m "feat: SQLite store for sessions, quotes, fills, rollups"
```

---

### Task 6: Adverse selection tracker

**Files:**
- Create: `mm_bot/paper/adverse.py`
- Test: `tests/test_adverse.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_adverse.py
from mm_bot.paper.adverse import AdverseSelectionTracker


def make_tracker(horizon_ms=5000):
    results = []
    t = AdverseSelectionTracker(horizon_ms, lambda ref, move: results.append((ref, move)))
    return t, results


def test_buy_fill_adverse_when_mid_drops():
    t, results = make_tracker()
    t.add_fill(ref=1, side="buy", mid_at_fill=60000.0, ts_ms=1_000_000)
    t.on_mid(59990.0, 1_004_999)  # before horizon: nothing
    assert results == []
    t.on_mid(59990.0, 1_005_000)  # at horizon: resolves
    assert results == [(1, 10.0)]  # bought, mid dropped 10 = adverse +10


def test_sell_fill_adverse_when_mid_rises():
    t, results = make_tracker()
    t.add_fill(ref=2, side="sell", mid_at_fill=60000.0, ts_ms=1_000_000)
    t.on_mid(60007.0, 1_005_000)
    assert results == [(2, 7.0)]


def test_favorable_moves_are_negative():
    t, results = make_tracker()
    t.add_fill(ref=3, side="buy", mid_at_fill=60000.0, ts_ms=1_000_000)
    t.on_mid(60004.0, 1_006_000)
    assert results == [(3, -4.0)]


def test_multiple_fills_resolve_in_order():
    t, results = make_tracker()
    t.add_fill(ref=1, side="buy", mid_at_fill=60000.0, ts_ms=1_000_000)
    t.add_fill(ref=2, side="buy", mid_at_fill=60010.0, ts_ms=1_002_000)
    t.on_mid(60000.0, 1_005_500)  # resolves ref 1 only
    assert [r[0] for r in results] == [1]
    t.on_mid(60000.0, 1_007_000)  # resolves ref 2
    assert [r[0] for r in results] == [1, 2]


def test_uses_first_mid_at_or_after_horizon():
    t, results = make_tracker()
    t.add_fill(ref=1, side="buy", mid_at_fill=60000.0, ts_ms=1_000_000)
    t.on_mid(59980.0, 1_009_000)  # first observation past horizon wins
    t.on_mid(59900.0, 1_010_000)
    assert results == [(1, 20.0)]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_adverse.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.paper.adverse'`

- [ ] **Step 3: Implement**

```python
# mm_bot/paper/adverse.py
"""Per-fill adverse selection: mid move against us, K seconds after the fill.

Positive = adverse (bought right before mid dropped, or sold right before it
rose). Resolved at the first mid observation at or after fill time + horizon,
using exchange timestamps only.
"""
from collections import deque


class AdverseSelectionTracker:
    def __init__(self, horizon_ms: int, on_result) -> None:
        self._horizon_ms = horizon_ms
        self._on_result = on_result  # sync callable(ref, adverse_move_usd)
        self._pending: deque = deque()  # (due_ms, ref, side, mid_at_fill)

    def add_fill(self, ref, side: str, mid_at_fill: float, ts_ms: int) -> None:
        self._pending.append((ts_ms + self._horizon_ms, ref, side, mid_at_fill))

    def on_mid(self, mid: float, ts_ms: int) -> None:
        while self._pending and self._pending[0][0] <= ts_ms:
            _, ref, side, mid_at_fill = self._pending.popleft()
            if side == "buy":
                move = mid_at_fill - mid
            else:
                move = mid - mid_at_fill
            self._on_result(ref, move)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_adverse.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/paper/adverse.py tests/test_adverse.py
git commit -m "feat: per-fill adverse selection tracker"
```

---

### Task 7: Paper engine (lanes + event routing)

**Files:**
- Create: `mm_bot/paper/engine.py`
- Test: `tests/test_engine.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_engine.py
from mm_bot.config import StrategyConfig
from mm_bot.feed.book import OrderBook
from mm_bot.feed.messages import BookChange, BookSnapshot, Trade
from mm_bot.paper.engine import PaperEngine, StrategyLane
from mm_bot.store.db import Store
from mm_bot.strategy.fixed_spread import FixedSpreadStrategy


def snapshot(ts, change_id=1000, bid=60000.0, ask=60000.5):
    return BookSnapshot(
        instrument="BTC-PERPETUAL", change_id=change_id, timestamp_ms=ts,
        bids=[(bid, 5000.0)], asks=[(ask, 4200.0)],
    )


def change(ts, change_id, prev, bid=60000.0):
    return BookChange(
        instrument="BTC-PERPETUAL", change_id=change_id, prev_change_id=prev,
        timestamp_ms=ts, bids=[("change", bid, 5000.0)], asks=[],
    )


def trade_ev(ts, price, amount=50.0, trade_id="t1"):
    return Trade(
        instrument="BTC-PERPETUAL", trade_id=trade_id, trade_seq=1,
        timestamp_ms=ts, price=price, amount=amount, direction="sell",
    )


def make_engine(tmp_path, half_spread=5.0):
    cfg = StrategyConfig(name="base", half_spread_usd=half_spread,
                         quote_size_usd=100.0, requote_interval_s=1.0)
    store = Store(tmp_path / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    book = OrderBook()
    lane = StrategyLane(FixedSpreadStrategy(cfg), cfg, store, "s1", adverse_horizon_ms=5000)
    engine = PaperEngine(book=book, lanes=[lane], store=store,
                         session_id="s1", rollup_interval_ms=60_000)
    return engine, book, lane, store


async def apply(engine, event):
    """Book events must hit the book before the engine, mirroring the live client."""
    engine.apply_book_event(event)
    await engine.on_event(event)


async def test_snapshot_triggers_first_quotes(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    # mid = 60000.25, half spread 5 -> bid 59995.0, ask 60005.5
    assert lane.sim.bid == (59995.0, 100.0)
    assert lane.sim.ask == (60005.5, 100.0)
    assert store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1


async def test_requote_respects_interval(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    await apply(engine, change(ts=1_000_500, change_id=1001, prev=1000))  # 0.5s: no requote
    assert store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
    await apply(engine, change(ts=1_001_000, change_id=1002, prev=1001))  # 1.0s: requote
    assert store.connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 2


async def test_trade_fill_updates_portfolio_and_persists(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    await engine.on_event(trade_ev(ts=1_000_100, price=59990.0, amount=50.0))
    assert lane.portfolio.position_usd == 50.0  # bid 59995 crossed
    row = store.connection.execute(
        "SELECT strategy, side, price, amount_usd, mid_at_fill FROM fills"
    ).fetchone()
    assert row == ("base", "buy", 59995.0, 50.0, 60000.25)


async def test_adverse_selection_resolved_and_stored(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    await engine.on_event(trade_ev(ts=1_000_100, price=59990.0))
    # mid moves down and 5s pass -> adverse resolves on next book event
    await apply(engine, change(ts=1_006_000, change_id=1001, prev=1000, bid=59980.0))
    adverse = store.connection.execute(
        "SELECT adverse_move_usd FROM fills"
    ).fetchone()[0]
    # mid_at_fill 60000.25, mid now (59980+60000.5)/2 = 59990.25, buy: 60000.25-59990.25
    assert adverse == 10.0


async def test_rollup_written_on_interval(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await apply(engine, snapshot(ts=1_000_000))
    await apply(engine, change(ts=1_061_000, change_id=1001, prev=1000))
    assert store.connection.execute("SELECT COUNT(*) FROM rollups").fetchone()[0] == 1


async def test_trades_before_book_ready_are_ignored(tmp_path):
    engine, book, lane, store = make_engine(tmp_path)
    await engine.on_event(trade_ev(ts=1_000_000, price=59990.0))  # no book yet
    assert lane.portfolio.fill_count == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.paper.engine'`

- [ ] **Step 3: Implement**

```python
# mm_bot/paper/engine.py
"""Paper trading engine: routes feed events through strategy lanes.

One lane per strategy: strategy + fill simulator + portfolio + adverse
selection tracker, all sharing the single live order book. All timing uses
exchange timestamps from the messages, never the local clock.
"""
import logging

from mm_bot.config import StrategyConfig
from mm_bot.feed.book import OrderBook
from mm_bot.feed.messages import BookChange, BookSnapshot, Trade
from mm_bot.paper.adverse import AdverseSelectionTracker
from mm_bot.paper.portfolio import Portfolio
from mm_bot.paper.sim import FillSimulator
from mm_bot.store.db import Store

log = logging.getLogger(__name__)


class StrategyLane:
    def __init__(
        self, strategy, cfg: StrategyConfig, store: Store, session_id: str,
        adverse_horizon_ms: int,
    ) -> None:
        self.strategy = strategy
        self.cfg = cfg
        self.portfolio = Portfolio()
        self.sim = FillSimulator(self._handle_fill)
        self.adverse = AdverseSelectionTracker(adverse_horizon_ms, store.set_adverse)
        self.quote_count = 0
        self.last_quote_ms: int | None = None
        self._store = store
        self._session_id = session_id
        self._current_mid: float | None = None

    def _handle_fill(self, fill) -> None:
        self.portfolio.apply_fill(fill)
        fill_id = self._store.record_fill(
            self._session_id, fill.timestamp_ms, self.strategy.name,
            side=fill.side, price=fill.price, amount_usd=fill.amount_usd,
            trade_id=fill.trade_id, mid_at_fill=self._current_mid,
        )
        self.adverse.add_fill(
            ref=fill_id, side=fill.side, mid_at_fill=self._current_mid,
            ts_ms=fill.timestamp_ms,
        )

    def on_mid(self, mid: float, ts_ms: int) -> None:
        self._current_mid = mid
        self.adverse.on_mid(mid, ts_ms)
        interval_ms = int(self.cfg.requote_interval_s * 1000)
        if self.last_quote_ms is None or ts_ms - self.last_quote_ms >= interval_ms:
            q = self.strategy.quotes(mid, self.portfolio.position_usd, ts_ms)
            self.sim.set_quotes(q.bid, q.ask, self.cfg.quote_size_usd)
            self._store.record_quote(
                self._session_id, ts_ms, self.strategy.name,
                q.bid, q.ask, self.cfg.quote_size_usd,
            )
            self.quote_count += 1
            self.last_quote_ms = ts_ms

    def on_trade(self, trade: Trade) -> None:
        if self._current_mid is None:
            return  # book not ready; never fill blind
        self.sim.on_trade(trade)

    def rollup(self, ts_ms: int, mid: float) -> None:
        self._store.record_rollup(
            self._session_id, ts_ms, self.strategy.name,
            position_usd=self.portfolio.position_usd,
            btc_cash=self.portfolio.btc_cash,
            equity_btc=self.portfolio.equity_btc(mid),
            equity_usd=self.portfolio.equity_usd(mid),
            mid=mid,
            fill_count=self.portfolio.fill_count,
            quote_count=self.quote_count,
        )


class PaperEngine:
    def __init__(
        self, book: OrderBook, lanes: list[StrategyLane], store: Store,
        session_id: str, rollup_interval_ms: int = 60_000,
    ) -> None:
        self._book = book
        self.lanes = lanes
        self._store = store
        self._session_id = session_id
        self._rollup_interval_ms = rollup_interval_ms
        self._last_rollup_ms: int | None = None

    async def on_event(self, event) -> None:
        match event:
            case BookSnapshot() | BookChange():
                # in live mode the feed client has already applied the event
                # to the shared book; in replay apply_book_event did
                mid = self._book.mid()
                if mid is None:
                    return
                ts = self._book.timestamp_ms
                for lane in self.lanes:
                    lane.on_mid(mid, ts)
                self._maybe_rollup(ts, mid)
            case Trade():
                for lane in self.lanes:
                    lane.on_trade(event)

    def apply_book_event(self, event) -> None:
        """Replay helper: apply a book event when no feed client owns the book."""
        match event:
            case BookSnapshot():
                self._book.apply_snapshot(event)
            case BookChange():
                self._book.apply_change(event)

    def _maybe_rollup(self, ts_ms: int, mid: float) -> None:
        if self._last_rollup_ms is None:
            self._last_rollup_ms = ts_ms
            return
        if ts_ms - self._last_rollup_ms >= self._rollup_interval_ms:
            for lane in self.lanes:
                lane.rollup(ts_ms, mid)
            self._last_rollup_ms = ts_ms
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_engine.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run full suite**

Run: `.venv\Scripts\python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add mm_bot/paper/engine.py tests/test_engine.py
git commit -m "feat: paper engine with per-strategy lanes, rollups, adverse wiring"
```

---

### Task 8: Replay harness + paper runner

**Files:**
- Create: `mm_bot/paper/replay.py`
- Create: `run_paper.py`
- Test: `tests/test_replay.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_replay.py
import json

from mm_bot.config import StrategyConfig
from mm_bot.paper.replay import replay_file
from mm_bot.store.db import Store


def msg_snapshot(change_id, ts, bid, ask):
    return {
        "jsonrpc": "2.0", "method": "subscription",
        "params": {"channel": "book.BTC-PERPETUAL.100ms", "data": {
            "type": "snapshot", "timestamp": ts, "instrument_name": "BTC-PERPETUAL",
            "change_id": change_id,
            "bids": [["new", bid, 5000.0]], "asks": [["new", ask, 4200.0]],
        }},
    }


def msg_trade(ts, price, amount, trade_id):
    return {
        "jsonrpc": "2.0", "method": "subscription",
        "params": {"channel": "trades.BTC-PERPETUAL.100ms", "data": [{
            "instrument_name": "BTC-PERPETUAL", "trade_id": trade_id,
            "trade_seq": 1, "timestamp": ts, "price": price,
            "amount": amount, "direction": "sell",
        }]},
    }


def write_jsonl(path, messages):
    with open(path, "w", encoding="utf-8") as fh:
        for m in messages:
            fh.write(json.dumps(m) + "\n")


def test_replay_produces_deterministic_fills(tmp_path):
    raw = tmp_path / "raw.jsonl"
    write_jsonl(raw, [
        msg_snapshot(1000, ts=1_000_000, bid=60000.0, ask=60000.5),
        # mid 60000.25; half spread 5 -> bid 59995.0 / ask 60005.5
        msg_trade(ts=1_000_100, price=59990.0, amount=50.0, trade_id="a"),  # fills bid
        msg_trade(ts=1_000_200, price=60010.0, amount=30.0, trade_id="b"),  # fills ask
    ])
    cfg = StrategyConfig(name="base", half_spread_usd=5.0, quote_size_usd=100.0)
    store = Store(tmp_path / "mm.sqlite")
    summary = replay_file(raw, [cfg], store, session_id="replay-1")
    rows = store.connection.execute(
        "SELECT side, price, amount_usd FROM fills ORDER BY id"
    ).fetchall()
    assert rows == [("buy", 59995.0, 50.0), ("sell", 60005.5, 30.0)]
    assert summary["base"]["fills"] == 2
    assert summary["base"]["position_usd"] == 20.0
    store.close()


def test_replay_is_reproducible(tmp_path):
    raw = tmp_path / "raw.jsonl"
    write_jsonl(raw, [
        msg_snapshot(1000, ts=1_000_000, bid=60000.0, ask=60000.5),
        msg_trade(ts=1_000_100, price=59990.0, amount=50.0, trade_id="a"),
    ])
    cfg = StrategyConfig(name="base")
    s1 = Store(tmp_path / "a.sqlite")
    s2 = Store(tmp_path / "b.sqlite")
    r1 = replay_file(raw, [cfg], s1, session_id="r1")
    r2 = replay_file(raw, [cfg], s2, session_id="r2")
    assert r1 == r2
    s1.close()
    s2.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_replay.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.paper.replay'`

- [ ] **Step 3: Implement replay**

```python
# mm_bot/paper/replay.py
"""Deterministic replay of a recorded raw JSONL session through the engine."""
import asyncio
import json
from pathlib import Path

from mm_bot.config import StrategyConfig
from mm_bot.feed.book import GapError, OrderBook
from mm_bot.feed.messages import BookChange, BookSnapshot, parse_message
from mm_bot.paper.engine import PaperEngine, StrategyLane
from mm_bot.store.db import Store
from mm_bot.strategy.fixed_spread import FixedSpreadStrategy


def build_strategy(cfg: StrategyConfig):
    if cfg.kind == "fixed_spread":
        return FixedSpreadStrategy(cfg)
    raise ValueError(f"unknown strategy kind: {cfg.kind}")


def replay_file(
    path: str | Path, strategy_cfgs, store: Store, session_id: str,
    adverse_horizon_ms: int = 5000,
) -> dict:
    store.start_session(session_id, 0, "replay", "{}")
    book = OrderBook()
    lanes = [
        StrategyLane(build_strategy(c), c, store, session_id, adverse_horizon_ms)
        for c in strategy_cfgs
    ]
    engine = PaperEngine(book=book, lanes=lanes, store=store, session_id=session_id)

    async def _run() -> None:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                for event in parse_message(json.loads(line)):
                    if isinstance(event, (BookSnapshot, BookChange)):
                        try:
                            engine.apply_book_event(event)
                        except GapError:
                            book.reset()
                            continue
                    await engine.on_event(event)

    asyncio.run(_run())
    mid = book.mid()
    return {
        lane.strategy.name: {
            "fills": lane.portfolio.fill_count,
            "quotes": lane.quote_count,
            "position_usd": lane.portfolio.position_usd,
            "equity_btc": lane.portfolio.equity_btc(mid) if mid else None,
        }
        for lane in lanes
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_replay.py -v`
Expected: 2 passed.

- [ ] **Step 5: Write the live paper runner**

```python
# run_paper.py
"""Milestone 2 entrypoint: live paper trading with configured strategies.

Usage: python run_paper.py [config.yaml]
"""
import asyncio
import json
import logging
import subprocess
import sys
import time
from dataclasses import asdict

from mm_bot.config import load_config
from mm_bot.feed.client import DeribitFeedClient
from mm_bot.feed.recorder import JsonlRecorder
from mm_bot.paper.engine import PaperEngine, StrategyLane
from mm_bot.paper.replay import build_strategy
from mm_bot.store.db import Store

log = logging.getLogger("run_paper")

REPORT_INTERVAL_S = 60


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    session_id = time.strftime("%Y%m%d-%H%M%S")
    recorder = JsonlRecorder(cfg.recorder.data_dir, session_id)
    store = Store(cfg.store.db_path)
    store.start_session(
        session_id, int(time.time() * 1000), git_commit(),
        json.dumps({"strategies": [asdict(s) for s in cfg.strategies]}),
    )
    lanes = [
        StrategyLane(
            build_strategy(c), c, store, session_id,
            adverse_horizon_ms=int(cfg.store.adverse_horizon_s * 1000),
        )
        for c in cfg.strategies
    ]
    client = DeribitFeedClient(cfg.feed, on_event=None, on_raw=recorder.record)
    engine = PaperEngine(
        book=client.book, lanes=lanes, store=store, session_id=session_id,
        rollup_interval_ms=cfg.store.rollup_interval_s * 1000,
    )
    client._on_event = engine.on_event  # engine consumes all feed events

    async def report() -> None:
        while True:
            await asyncio.sleep(REPORT_INTERVAL_S)
            mid = client.book.mid()
            for lane in lanes:
                log.info(
                    "%s: pos_usd=%.1f fills=%d quotes=%d equity_usd=%s mid=%s",
                    lane.strategy.name,
                    lane.portfolio.position_usd,
                    lane.portfolio.fill_count,
                    lane.quote_count,
                    f"{lane.portfolio.equity_usd(mid):.4f}" if mid else None,
                    mid,
                )
            recorder.flush()

    log.info("paper session %s (db=%s)", session_id, cfg.store.db_path)
    try:
        await asyncio.gather(client.run(), report())
    finally:
        recorder.close()
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
```

Design notes:
- `DeribitFeedClient` already applies book events to its own book before
  dispatching to `on_event`, so the engine must NOT call `apply_book_event`
  in live mode; that method exists only for replay where no client owns the
  book. That is why they are separate methods.
- `client._on_event = engine.on_event` after construction is deliberate: the
  engine needs the client's book, and the client needs the event callback.
  Keep it exactly as written.

- [ ] **Step 6: Verify runner imports and full suite passes**

Run: `.venv\Scripts\python -c "import run_paper"` — expected exit 0.
Run: `.venv\Scripts\python -m pytest -q` — expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add mm_bot/paper/replay.py run_paper.py tests/test_replay.py
git commit -m "feat: deterministic replay harness and live paper runner"
```

---

### Task 9: Live verification (Fable, not a subagent)

Performed by the main session. Subagents stop after Task 8.

- [ ] **Step 1: Replay the recorded smoke session**

Replay a real recorded raw file from `data/` through `replay_file` with the
configured strategies into a scratch database. Expected: runs without error,
prints per-strategy summary with plausible counts (quotes > 0; fills may be 0
or small on a short file, that is fine).

- [ ] **Step 2: Live paper smoke run (~5 minutes)**

Run `run_paper.py` for ~5 minutes. Expected: per-strategy stats lines every
60s, no tracebacks, quotes accumulating, mid tracking live BTC.

- [ ] **Step 3: Inspect the database**

Check sessions row exists with git commit; quotes table growing (~1 row/s per
strategy); any fills have sane prices (at our quoted prices); rollups
appearing each minute.

- [ ] **Step 4: Milestone gate**

Milestone 2 done when steps 1-3 pass and full suite is green.

---

## Execution notes

- Sonnet 5 subagents implement Tasks 1-8 sequentially (per-task dispatch, code plus tests). Fable reviews each diff and performs Task 9.
- Deribit trade `amount` is USD notional for BTC-PERPETUAL; all sizes here are USD notional. Contracts = amount/10 (not needed in code, sizes stay in USD).
- Timestamps: exchange ms everywhere. Never `time.time()` inside engine/lane/sim/adverse logic (only in runners for session ids and metadata).
