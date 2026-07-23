# tests/conftest.py
"""Shared fixtures. pykx-dependent tests must skip, never fail, when kdb+ is
unavailable: the core 131-test suite has to stay green on machines with no
license (CI, fresh clones)."""
import os

import pytest


def _pykx_licensed() -> bool:
    os.environ.setdefault("PYKX_NOQCE", "1")
    try:
        import pykx
    except Exception:
        return False
    return bool(getattr(pykx, "licensed", False))


requires_pykx = pytest.mark.skipif(
    not _pykx_licensed(), reason="pykx not installed or not licensed"
)
