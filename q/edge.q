/ q/edge.q
/ Per-fill edge decomposition, closed forms proven equal to the Python
/ Portfolio round-trip (see plan 2026-07-23, "Math locked in"):
/   spread capture: buy U*(m-p)%p, sell U*(p-m)%p
/   adverse selection: neg U*adv%p (both sides)
/ Python (mm_bot/research/edge.py) stays the oracle; tests require agreement.

decompose:{[f]
  f:select tsMs, strat, side, p:price, U:amtUsd, m:midAtFill, adv:advMoveUsd
    from f where not null advMoveUsd;
  update scUsd:U*?[side=`buy;(m-p)%p;(p-m)%p], asUsd:neg U*adv%p from f};

edgeByDay:{[f]
  select scUsd:sum scUsd, asUsd:sum asUsd, n:count i
    by strat, dayIdx:tsMs div 86400000 from decompose f}
