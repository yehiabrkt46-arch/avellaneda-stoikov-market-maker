"""Append-only raw message recorder, one JSONL file per session."""
import json
from pathlib import Path


class JsonlRecorder:
    def __init__(self, data_dir: str | Path, session_id: str) -> None:
        self.path = Path(data_dir) / f"raw-{session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def record(self, msg: dict) -> None:
        self._fh.write(json.dumps(msg, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
