from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class FeedConfig:
    ws_url: str = "wss://www.deribit.com/ws/api/v2"
    instrument: str = "BTC-PERPETUAL"
    book_interval: str = "100ms"
    heartbeat_interval_s: int = 30
    stale_data_timeout_s: float = 10.0
    reconnect_initial_delay_s: float = 1.0
    reconnect_max_delay_s: float = 60.0


@dataclass(frozen=True)
class RecorderConfig:
    data_dir: str = "data"


@dataclass(frozen=True)
class StrategyConfig:
    kind: str = "fixed_spread"
    name: str = "fixed_spread"
    half_spread_usd: float = 5.0
    quote_size_usd: float = 100.0
    tick_size: float = 0.5
    requote_interval_s: float = 1.0
    gamma: float = 0.001
    horizon_s: float = 60.0
    vol_lambda: float = 0.97
    vol_min_dt_s: float = 1.0
    vol_min_samples: int = 30
    k_window_s: float = 1800.0
    k_min_trades: int = 50
    ofi_beta: float = 0.0
    ofi_scale: float = 0.0
    ofi_window_s: float = 1.0
    inventory_cap_usd: float = 500.0
    max_drawdown_usd: float = 100.0


@dataclass(frozen=True)
class StoreConfig:
    db_path: str = "data/mm.sqlite"
    rollup_interval_s: int = 60
    adverse_horizon_s: float = 5.0
    stale_quote_pull_s: float = 10.0


@dataclass(frozen=True)
class Config:
    feed: FeedConfig
    recorder: RecorderConfig
    strategies: tuple[StrategyConfig, ...]
    store: StoreConfig


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
