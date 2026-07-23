# tests/test_ofi.py
"""OFI on hand-computed book sequences; regression on a planted linear signal."""
import math

from tests.conftest import requires_pykx


@requires_pykx
def test_ofi_hand_computed():
    import pykx
    from mm_bot.research.qsession import get_q

    q = get_q(scripts=("ofi.q",))
    # Hand computation against the formula in ofi.q's header:
    # row2 (1100): bid 100->100 (>= and <= both true): e_b = 7 - 5 = 2
    #              ask 101->101 (<= and >= both true): e_a = 3 - 3 = 0;  e = 2
    # row3 (1200): bid 100->99 (down): e_b = 0 - 7 = -7; ask same: e_a = 3-3=0; e = -7
    # row4 (1300): bid same 99: e_b = 4 - 4 = 0
    #              ask 101->100 (down): e_a = 6 - 0 = 6;  e = 0 - 6 = -6
    rows = [
        (1000, 100.0, 5.0, 101.0, 3.0),
        (1100, 100.0, 7.0, 101.0, 3.0),
        (1200,  99.0, 4.0, 101.0, 3.0),
        (1300,  99.0, 4.0, 100.0, 6.0),
    ]
    q["bookT"] = pykx.Table(data={
        "tsMs": [r[0] for r in rows], "bid": [r[1] for r in rows],
        "bsize": [r[2] for r in rows], "ask": [r[3] for r in rows],
        "asize": [r[4] for r in rows]})
    e = q("exec e from ofiEvents bookT").py()
    assert e == [2.0, -7.0, -6.0]


@requires_pykx
def test_ols_recovers_planted_line():
    from mm_bot.research.qsession import get_q

    q = get_q(scripts=("ofi.q",))
    q("x:0.01*til 500; y:3.0+2.0*x")
    m = q("fitOls[x;y]").py()
    assert abs(m["alpha"] - 3.0) < 1e-9
    assert abs(m["beta"] - 2.0) < 1e-9
    ev = q("evalOls[fitOls[x;y];x;y]").py()
    assert ev["r2oos"] > 0.999999


@requires_pykx
def test_fwd_shift_pads_with_null():
    from mm_bot.research.qsession import get_q

    q = get_q(scripts=("ofi.q",))
    out = q("fwdShift[2; 1.0 2.0 3.0 4.0]").py()
    assert out[:2] == [3.0, 4.0]
    assert math.isnan(out[2]) and math.isnan(out[3])
