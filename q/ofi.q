/ q/ofi.q
/ Order-flow imbalance (Cont, Kukanov, Stoikov 2014): per book update
/   e = 1[b_t>=b_{t-1}]*q^b_t - 1[b_t<=b_{t-1}]*q^b_{t-1}
/     - 1[a_t<=a_{t-1}]*q^a_t + 1[a_t>=a_{t-1}]*q^a_{t-1}
/ summed over updates in a time bucket. Predictor of short-horizon mid moves.

ofiEvents:{[t]
  t:update pb:prev bid, pa:prev ask, pbs:prev bsize, pas:prev asize from
    `tsMs xasc select tsMs, bid, bsize, ask, asize from t;
  t:1_ t;
  update e:((?[bid>=pb;bsize;0f])-?[bid<=pb;pbs;0f])
          -((?[ask<=pa;asize;0f])-?[ask>=pa;pas;0f]) from t}

ofiBuckets:{[t;bucketMs]
  e:ofiEvents t;
  select ofi:sum e, mid:last 0.5*bid+ask by bkt:bucketMs xbar tsMs from e}

fwdShift:{[h;v] (h _ v),h#0n}

withFwdRet:{[b;h] update fwdRet:(fwdShift[h;mid]%mid)-1 from b}

fitOls:{[x;y]
  ok:where (not null x) and not null y; x:x ok; y:y ok;
  b:cov[x;y]%var x; a:avg[y]-b*avg x;
  `alpha`beta`n!(a;b;count ok)}

evalOls:{[m;x;y]
  ok:where (not null x) and not null y; x:x ok; y:y ok;
  pred:m[`alpha]+m[`beta]*x;
  sse:sum d*d:y-pred; tot:sum d2*d2:y-avg y;
  `r2oos`hitRate`n!((1-sse%tot); avg (signum pred)=signum y; count ok)}
