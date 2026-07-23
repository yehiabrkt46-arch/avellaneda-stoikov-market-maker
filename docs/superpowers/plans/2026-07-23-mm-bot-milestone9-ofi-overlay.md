# Milestone 9: OFI Overlay in Avellaneda-Stoikov Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Skew the A-S reservation price by the OFI-predicted forward mid move (beta fitted on train days in M8), sweep the skew scale over the recorded window, select on train days, and report the held-out test score against an identically-run no-overlay control.

**Architecture:** Three additive pieces: an online `OfiEstimator` (same Cont-Kukanov-Stoikov formula as `q/ofi.q`, incremental with a trailing time window), a new no-op `observe_book` strategy hook wired through the engine's book-event path, and three optional `StrategyConfig` fields consumed by `AvellanedaStoikovStrategy` (all defaulting to zero effect, so every existing test and replay stays byte-identical). The sweep driver mirrors `scripts/run_param_sweep.py` (resumable, one single-lane replay pass per candidate) and runs on the VPS like M7.

**Tech Stack:** Pure Python (no pykx anywhere in M9 runtime), existing replay engine, VPS pm2 one-shot job.

---

## Locked facts

- Tuned A-S base config (M7 winner): `gamma=0.002`, `horizon_s=120`, other params from `config.yaml`.
- Train-fit betas from M8 (`data/ofi-results.json`, train days 20645-20652 only): `h=5s: beta = 1.783e-10`, `h=1s: beta = 6.863e-11` per unit OFI (book sizes in USD contracts), predicting *relative* forward mid return. Alpha is dropped (a constant shift is a bias term, negligible; disclose in README).
- Overlay formula, applied only when the A-S model is warm (never during warmup fallback): `reservation = mid - q*gamma*sigma2*tau + ofi_scale * ofi_beta * ofi * mid`.
- OFI window = 1000 ms trailing (matches the M8 study bucket).
- Candidates (5 single-lane passes, session ids in parens):
  1. control `ofi_scale=0` (`ofi-c0`) — must reproduce M7's `sweep-avellaneda_stoikov-g0.002-h120.0` scores (train -68.65578, test -19.42465); built-in determinism check.
  2. `beta_5s`, scale 0.5 (`ofi-b5-s0.5`)
  3. `beta_5s`, scale 1.0 (`ofi-b5-s1.0`)
  4. `beta_5s`, scale 2.0 (`ofi-b5-s2.0`)
  5. `beta_1s`, scale 1.0 (`ofi-b1-s1.0`)
- Score = sum over fills of spread_capture + adverse_selection per day (`mm_bot/research/edge.py::aggregate_fill_edge_by_day`), split via `train_test_split_days(sorted(days))` exactly as `run_param_sweep.py --report` does. Selection on train days; test days reported only.
- Raw file on VPS: `/opt/mm-bot/data/raw-20260711-231533.jsonl`. Local copy: `data/vps-pull-20260721/raw-20260711-231533.jsonl`.
- Repo conventions: no commit trailers, no em dashes in README prose.

## File structure

```
mm_bot/strategy/estimators.py    MOD  add OfiEstimator (additive)
mm_bot/strategy/base.py          MOD  add no-op observe_book hook (additive)
mm_bot/paper/engine.py           MOD  call lane.on_book -> strategy.observe_book on book events (additive)
mm_bot/strategy/avellaneda_stoikov.py  MOD  consume ofi fields
mm_bot/config.py                 MOD  3 new StrategyConfig fields, zero-effect defaults
scripts/run_ofi_overlay_sweep.py NEW  5-candidate resumable sweep + --report
tests/test_estimators.py         MOD  OfiEstimator tests
tests/test_engine_hooks.py       MOD  observe_book wiring test
tests/test_avellaneda_stoikov.py MOD  overlay shift tests
README.md                        MOD  final task: replace status sentence with measured result
```

---

### Task 1: OfiEstimator (TDD)

**Files:** Modify `mm_bot/strategy/estimators.py`, `tests/test_estimators.py`

- [ ] **Step 1: Failing tests** (append to `tests/test_estimators.py`; match its existing import style):

```python
def test_ofi_matches_hand_computed_sequence():
    # Same sequence as tests/test_ofi.py (q side): e values 2, -7, -6
    o = OfiEstimator(window_ms=10_000)
    o.observe(100.0, 5.0, 101.0, 3.0, 1000)   # first obs: no prev, e undefined
    assert o.ofi() == 0.0
    o.observe(100.0, 7.0, 101.0, 3.0, 1100)   # e = +2
    assert o.ofi() == 2.0
    o.observe(99.0, 4.0, 101.0, 3.0, 1200)    # e = -7
    assert o.ofi() == -5.0
    o.observe(99.0, 4.0, 100.0, 6.0, 1300)    # e = -6
    assert o.ofi() == -11.0


def test_ofi_window_eviction():
    o = OfiEstimator(window_ms=150)
    o.observe(100.0, 5.0, 101.0, 3.0, 1000)
    o.observe(100.0, 7.0, 101.0, 3.0, 1100)   # e = +2 at ts 1100
    o.observe(99.0, 4.0, 101.0, 3.0, 1200)    # e = -7 at ts 1200
    # window is (ts - 150, ts]: at ts 1300 the +2 event (1100) has left
    o.observe(99.0, 4.0, 100.0, 6.0, 1300)    # e = -6 at ts 1300
    assert o.ofi() == -13.0
```

- [ ] **Step 2: Run, verify ImportError:** `.venv/Scripts/python.exe -m pytest tests/test_estimators.py -q`

- [ ] **Step 3: Implement** (append to `mm_bot/strategy/estimators.py`):

```python
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
```

- [ ] **Step 4: Green + full suite:** `pytest tests/test_estimators.py -q` then `pytest -q` (expect prior counts + 2).

- [ ] **Step 5: Commit:** `git add mm_bot/strategy/estimators.py tests/test_estimators.py && git commit -m "feat: online OFI estimator matching the q study formula"`

### Task 2: observe_book hook through engine (TDD)

**Files:** Modify `mm_bot/strategy/base.py`, `mm_bot/paper/engine.py`, `tests/test_engine_hooks.py`

- [ ] **Step 1: Failing test** (append to `tests/test_engine_hooks.py`; read the file first and reuse its existing fixture/strategy-stub pattern for driving book events through `PaperEngine.on_event`):

The test: a stub strategy records `observe_book` calls; feed a snapshot event through the engine; assert the stub received `(best_bid, best_bid_size, best_ask, best_ask_size, book.timestamp_ms)` once per book event, and that a one-sided book produces no `observe_book` call (engine returns early on `mid() is None`).

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** In `mm_bot/strategy/base.py`, add to `Strategy`:

```python
    def observe_book(
        self, bid: float, bid_size: float, ask: float, ask_size: float, ts_ms: int
    ) -> None:
        pass
```

In `mm_bot/paper/engine.py`: add to `StrategyLane`:

```python
    def on_book(
        self, bid: float, bid_size: float, ask: float, ask_size: float, ts_ms: int
    ) -> None:
        self.strategy.observe_book(bid, bid_size, ask, ask_size, ts_ms)
```

In `PaperEngine.on_event`, inside the `BookSnapshot() | BookChange()` case, after the `mid is None` early return and before the `lane.on_mid` loop:

```python
                bb, ba = self._book.best_bid(), self._book.best_ask()
                bbs, bas = self._book.best_bid_size(), self._book.best_ask_size()
                for lane in self.lanes:
                    lane.on_book(bb, bbs, ba, bas, ts)
```

(mid is not None guarantees both sides exist, so the size accessors cannot return None here.)

- [ ] **Step 4: Green + full suite** (all existing tests must pass unchanged — the hook is a no-op for existing strategies).

- [ ] **Step 5: Commit:** `git commit -m "feat: top-of-book observation hook through engine lanes"` (add the three files).

### Task 3: A-S overlay params (TDD)

**Files:** Modify `mm_bot/config.py`, `mm_bot/strategy/avellaneda_stoikov.py`, `tests/test_avellaneda_stoikov.py`

- [ ] **Step 1: Config fields.** Add to `StrategyConfig` (after `k_min_trades`):

```python
    ofi_beta: float = 0.0
    ofi_scale: float = 0.0
    ofi_window_s: float = 1.0
```

- [ ] **Step 2: Failing tests** (append to `tests/test_avellaneda_stoikov.py`; read the file first — it has an overrides-merging fixture, reuse it):

Test A (`test_ofi_skew_shifts_reservation`): warm A-S strategy with `ofi_beta=1e-10, ofi_scale=1.0`, drive `observe_book` calls producing a known OFI (after the 2nd observation of the hand-computed sequence, OFI = +2.0), compare `quotes(mid, 0, ts)` to an identical strategy with `ofi_scale=0.0`: both bid and ask shifted up by `1e-10 * 2.0 * mid` before tick rounding (assert with one-tick tolerance).
Test B (`test_ofi_zero_scale_identical`): `ofi_scale=0.0` with nonzero observed OFI produces exactly the same QuotePair as a strategy that never saw `observe_book`.
Test C (`test_ofi_not_applied_during_warmup`): cold estimators + nonzero OFI: quotes equal the plain warmup fallback (mid +- half_spread_usd).

Getting the strategy warm: copy the pattern the existing tests in that file already use.

- [ ] **Step 3: Implement.** In `AvellanedaStoikovStrategy.__init__`:

```python
        self._ofi = OfiEstimator(window_ms=int(cfg.ofi_window_s * 1000))
```

Add:

```python
    def observe_book(self, bid, bid_size, ask, ask_size, ts_ms) -> None:
        self._ofi.observe(bid, bid_size, ask, ask_size, ts_ms)
```

In `quotes()`, warm branch only, after `reservation = ...`:

```python
        if self._cfg.ofi_scale != 0.0:
            reservation += self._cfg.ofi_scale * self._cfg.ofi_beta * self._ofi.ofi() * mid
```

Import `OfiEstimator` alongside the existing estimator imports.

- [ ] **Step 4: Green + full suite.** All prior tests unchanged (defaults are zero-effect).

- [ ] **Step 5: Commit:** `git commit -m "feat: OFI reservation-price skew in Avellaneda-Stoikov (zero-effect by default)"`

### Task 4: overlay sweep driver + local smoke

**Files:** Create `scripts/run_ofi_overlay_sweep.py`

- [ ] **Step 1: Read `scripts/run_param_sweep.py` end to end.** The new driver mirrors it: same resumability (skip candidate if its session_id already in the output db's sessions table), same raw-file path probing (`data/vps-pull-20260721/` then `data/`), same single-candidate `replay_file` invocation with a one-element strategy list, same `--report` structure. Differences only:
  - Output db: `data/ofi-overlay-results.sqlite`.
  - Candidates: exactly the 5 from "Locked facts", built as `StrategyConfig` replacements over the config.yaml A-S entry with `gamma=0.002, horizon_s=120.0` plus per-candidate `ofi_beta`/`ofi_scale` (betas hardcoded with a comment citing data/ofi-results.json + scripts/run_ofi_study.py as provenance).
  - Session ids: `ofi-c0`, `ofi-b5-s0.5`, `ofi-b5-s1.0`, `ofi-b5-s2.0`, `ofi-b1-s1.0`.
  - `--report`: per-candidate train/test totals via `aggregate_fill_edge_by_day` + `train_test_split_days` (same invocation as run_param_sweep.py), printed train-desc, PLUS a comparison block: control `ofi-c0` train/test vs best-by-train overlay candidate, and a printed PASS/FAIL of whether `ofi-c0` matches the M7 published numbers (train -68.65578, test -19.42465, tolerance 1e-3) — print only, no sys.exit.
  - An `--only <session_id>` flag to run a single candidate (used for smoke).

- [ ] **Step 2: Local smoke.** Create a 300k-line slice of the raw file in the scratchpad, run the driver with `--raw <slice> --db <scratch>/smoke.sqlite --only ofi-b5-s1.0`, verify the session lands with nonzero fills and that rerunning skips it (resumability). Delete scratch outputs. Do NOT run the full file locally.

- [ ] **Step 3: Full suite green, commit:** `git add scripts/run_ofi_overlay_sweep.py && git commit -m "feat: OFI overlay sweep driver (5 candidates, resumable, control determinism check)"`

### Task 5: VPS run (orchestrator-owned, not a subagent task)

- [ ] Push branch, deploy to VPS per `deploy/DEPLOY.md` pattern (git archive tarball + scp), start as pm2 one-shot `mm-bot-ofi-sweep` (`--no-autorestart`), ~15-20 min x 5 candidates sequential.
- [ ] On completion: run `--report` on the VPS, scp `data/ofi-overlay-results.sqlite` back to local `data/`.

### Task 6: README result + push (orchestrator-owned)

- [ ] Replace the "Status: running now..." sentence of the OFI overlay README section with the measured outcome: control reproduction statement, best candidate by train days, its held-out test score vs control, honest interpretation (improvement or not; grid-edge caveat if the best scale is 2.0 or the h1s beta). No em dashes. Run both test suites. Commit, merge `milestone-9-ofi-overlay` to main, push.

## Self-review notes

- Zero-effect defaults keep all existing tests and replay determinism intact; only additive hooks.
- Control candidate doubles as an end-to-end determinism check against M7 published numbers.
- Betas are train-day-fit only; selection on train days; test days reported once. Alpha dropped, disclosed.
- Scope cuts: no multi-level OFI, no live run, no beta re-fitting inside the sweep.
