/ q/report.q
/ Sweep report: per-candidate per-day edge score (spread capture + adverse
/ selection, same definition as scripts/run_param_sweep.py --report), then
/ walk-forward totals per candidate: train-day sum picks the winner, held-out
/ test-day sum reported alongside.

scoreByDay:{[f]
  select score:sum scUsd+asUsd by session, dayIdx:tsMs div 86400000
    from update scUsd:U*?[side=`buy;(m-p)%p;(p-m)%p], asUsd:neg U*adv%p
    from select tsMs, session, side, p:price, U:amtUsd, m:midAtFill, adv:advMoveUsd
    from f where not null advMoveUsd}

report:{[s;trainDays;testDays]
  t:select train:sum score by session from s where dayIdx in trainDays;
  h:select test:sum score by session from s where dayIdx in testDays;
  `train xdesc 0! t lj h}
