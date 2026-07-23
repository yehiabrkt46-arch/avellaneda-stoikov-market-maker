/ q/verify.q
/ Independent recomputation of fill-time mid and forward mid from the top
/ table via asof joins. midAtFill should reproduce the engine's stored value
/ almost everywhere; advMove uses a next-observation convention in the
/ engine, so aj (last-at-or-before) differs at feed gaps; report, don't assert.

mids:{[] select sym, tsMs, mid:0.5*bid+ask from top}

checkMidAtFill:{[]
  f:select sym, tsMs, strat, side, midAtFill from fill;
  j:aj[`sym`tsMs; f; mids[]];
  select nFills:count i,
         nExact:sum 1e-9>abs midAtFill-mid,
         maxAbsDiff:max abs midAtFill-mid
    by strat from j}

checkAdvMove:{[horizonMs]
  f:select sym, tsMs, strat, side, midAtFill, advMoveUsd from fill
    where not null advMoveUsd;
  fwd:select sym, tsMs, fwdMid:mid from mids[];
  j:aj[`sym`tsMs; update tsMs:tsMs+horizonMs from f; fwd];
  j:update ajMove:?[side=`buy; midAtFill-fwdMid; fwdMid-midAtFill] from j;
  d:select strat, ad:abs advMoveUsd-ajMove from j;
  select nFills:count i, p50AbsDiff:med ad, maxAbsDiff:max ad by strat from d}
