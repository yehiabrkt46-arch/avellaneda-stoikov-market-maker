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
class Config:
    feed: FeedConfig
    recorder: RecorderConfig


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return Config(
        feed=FeedConfig(**raw.get("feed", {})),
        recorder=RecorderConfig(**raw.get("recorder", {})),
    )
