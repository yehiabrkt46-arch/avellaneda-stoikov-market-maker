"""Embedded q bootstrap for the research layer.

Central place for the licensed-pykx check and for loading the repo's .q
scripts, so every script and test gets the same failure message instead of
a pykx stack trace.
"""
import os
from pathlib import Path

Q_DIR = Path(__file__).resolve().parent.parent.parent / "q"


def get_q(scripts: tuple[str, ...] = ()):
    """Return the embedded pykx q instance with the given q/ scripts loaded.

    Raises RuntimeError with an actionable message if pykx is missing or
    unlicensed.
    """
    os.environ.setdefault("PYKX_NOQCE", "1")
    try:
        import pykx
    except ImportError as exc:
        raise RuntimeError(
            "pykx is not installed; run .venv/Scripts/pip install pykx"
        ) from exc
    if not getattr(pykx, "licensed", False):
        raise RuntimeError(
            "pykx is unlicensed; embedded q needs a (free) KX personal "
            "license: https://kx.com/kdb-personal-edition-download/"
        )
    for name in scripts:
        pykx.q((Q_DIR / name).read_text(encoding="utf-8"))
    return pykx.q
