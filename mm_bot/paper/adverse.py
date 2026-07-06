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
