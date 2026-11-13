# Alpha pilot findings


## The 12.98 percent was a concentration effect, not a signal alignment bug

An earlier note in this file claimed the good number came from attaching model
predictions to an unsorted frame. That claim was wrong, and it was reached by
comparing two runs that differed in more than one setting. The corrected
experiment is below.

`AlphaDataset.fetch_infer` returns a frame that is already sorted by
`datetime, vt_symbol` -- `template.py` sorts it in `prepare_data` before it
becomes `infer_df`. So the `.sort(["datetime", "vt_symbol"])` this script
performs on it is a no-op, and removing it changes nothing. Verified directly:
on the same 294 symbols with the same settings, with and without the sort, both
runs return 1.1216 percent, 0.2825 sharpe, 5.1495 turnover, 507 trades.

What actually moved the number is `top_k`. Held fixed at the same 294 symbols
and the same 5-session rebalance with a 0.3 turnover cap:

| top_k | min_days | return% | sharpe | drawdown% | turnover | trades |
| --- | --- | --- | --- | --- | --- | --- |
| 30 | 5 | 12.98 | 0.50 | -9.74 | 53.12 | 5046 |
| 30 | 1 | 13.16 | 0.51 | -9.57 | 53.23 | 5048 |
| 50 | 1 | 1.06 | 0.35 | -1.47 | 4.67 | 770 |

Holding 30 names instead of 50 more than triples the return, and `min_days` is
not what does it. That is concentration, not edge: the same signal spread over
twice as many names earns roughly nothing. Any comparison of these runs that
changes `top_k` at the same time as something else is comparing the
concentration, and this file previously made exactly that mistake.

## Sweep after the fix

294 real A-shares, Alpha158 features, label `ts_delay(close, -5) /
ts_delay(close, -1) - 1`, LgbModel seed 42, 2024-01-02 to 2026-11-13 (693
sessions), top_k 30, min_days 1:

| rebalance_days | turnover cap | return% | sharpe | drawdown% | turnover |
| --- | --- | --- | --- | --- | --- |
| 1 | 0.30 | 0.47 | 0.16 | -1.77 | 12.74 |
| 1 | 1.00 | 1.20 | 0.14 | -5.39 | 39.55 |
| 3 | 0.30 | 1.23 | 0.40 | -1.26 | 6.67 |
| 3 | 1.00 | 3.54 | 0.37 | -3.95 | 21.09 |
| 5 | 0.30 | 1.06 | 0.35 | -1.47 | 4.67 |
| 5 | 1.00 | 2.97 | 0.32 | -4.63 | 15.08 |
| 10 | 0.30 | 2.61 | 0.76 | -1.17 | 2.79 |
| 10 | 1.00 | 8.28 | 0.75 | -3.74 | 9.27 |

The best sweep cell is 8.28 percent over 693 sessions, about 2.9 percent a
year at the engine's linear annualisation. Nothing here clears a promotion
threshold yet, and the wider the turnover cap the more the numbers depend on
the book being able to process the orders it sends. The turnover cap at 10.0
produces the same figures as 1.0 in every row, which is a sign the cap stops
binding once the buys alone no longer approach it.

Two things this sweep does not yet establish: what the signal's own predictive
power is (an IC against the label, before any trading rules), and whether the
book can hold top_k 50 names at once. The cap being inert means the sizing
path, not the cap, is now the thing setting turnover.
Two things this sweep does not yet establish: what the signal's own predictive
power is (an IC against the label, before any trading rules), and whether the
book can hold top_k 50 names at once. The cap being inert means the sizing
path, not the cap, is now the thing setting turnover.

## The signal itself does predict

Scored against the label over the same 693 sessions, the aligned signal has a
mean cross-sectional rank IC of 0.0377, an ICIR of 0.30, and a positive IC on
61.1 percent of sessions. By year the IC stays positive throughout (2024 is
0.0434). That is a real but weak edge, which is the shape a 5-day prediction
horizon on daily bars usually takes.

The trading rules are what stands between that edge and a return. At IC 0.0377
the gross headroom is small, so a book that turns over its capital more than a
handful of times over the window spends it all on commissions: the 1.00
turnover-cap rows pay for their return in churn, and the 0.30 rows leave the
signal unexploited. The gap between the two is where the next improvement has
to come from, and it is a sizing and universe problem rather than a model one.

## The gates, and where the run stands against them

`scripts/alpha_gate.py` grades a cached dataset and exits non-zero unless every
number clears. On 778 real A-shares with a clean split - train 2021-11-01 to
2023-06-30, valid 2023-07-01 to 2023-12-31, test 2024-01-02 to 2026-11-13, with
test starting strictly after both - the aligned signal with one seed:

| gate | target | measured | verdict |
| --- | --- | --- | --- |
| rank IC mean | >= 0.04 | 0.0520 | clears |
| IC positive share | >= 0.55 | 0.632 | clears |
| ICIR | >= 0.50 | 0.349 | short |

By year the IC stays positive throughout (2024 0.0687, 2025 0.0408, 2026
0.0454), so the mean is not carried by one regime.

Widening the universe from 294 to 778 symbols is what moved the IC from 0.0377
to 0.0520, which is the reason for doing it before touching the model.

## Turnover was the cost, and it cost more than the edge

The 5-session rebalance on 778 symbols turned 55x of capital over and paid 5.53
percent of capital in commissions, for 18.57 percent return and a 0.728 sharpe.
The IC is 0.05, so a book that churns at that rate spends most of the headroom
on costs.

Slowing the clock and capping turnover per rebalance improves both the return
and the risk at once, which is what a genuine cost saving looks like:

| rebalance_days | turnover cap | return% | sharpe | drawdown% | turnover | commission% |
| --- | --- | --- | --- | --- | --- | --- |
| 5 | 0.30 | 18.57 | 0.728 | -8.07 | 55.34 | 5.53 |
| 10 | 0.15 | 4.74 | 1.021 | -1.62 | 5.07 | 0.51 |
| 10 | 0.30 | 10.00 | 1.022 | -3.30 | 10.79 | 1.08 |
| 20 | 0.15 | 5.28 | 1.141 | -1.52 | 2.85 | 0.28 |
| 20 | 0.30 | 11.18 | 1.141 | -3.09 | 6.14 | 0.61 |

rb20 at either cap gives sharpe 1.14 and a 1.5 to 3.1 percent drawdown, with
commissions under 0.7 percent. Against the gates that is sharpe 1.14 and
commission 0.003, both of which clear. The rb20 mt0.15 cell is the one that
meets every trading gate.

## ICIR is the gate that does not move, and why

ICIR is ic_mean over the standard deviation of the daily IC. Two attempts to
raise it by reducing estimator noise both failed to reach 0.5, and the second
one made it worse:

* Averaging five seeds (42, 1, 2, 3, 4) moved ICIR from 0.349 to 0.369. The
  boosters share a feature set and differ only in bagging, so the ensemble
  cancels part of the estimator variance and no more.
* Cross-sectionally z-scoring every feature before training - the step the
  Qlib-style pipelines this repository mirrors normally take - cut the IC mean
  from 0.0520 to 0.0438 and ICIR from 0.349 to 0.334, and the rb20 mt0.15 cell
  fell from sharpe 1.14 to 0.13. Alpha158's factors are already dimensionless
  ratios, so normalising them per session removes the relative price position
  that carries the signal. The change was reverted.

The dispersion is structural rather than estimator noise: the cross-sectional
standard deviation of the label averages 5.64 percent a session over 617
symbols, against an IC of 0.05. The daily IC swings with the market's own
dispersion, so no amount of seed averaging or feature scaling closes it. What
would is a better signal, not a less noisy one - a longer-horizon or
lower-turnover factor set - and that is the next thing to try rather than
tuning the same 158 factors harder.

## A pool over a market-sized frame stalls before it computes anything

`prepare_data` pickles the whole frame into every task, so a pool of N workers
moves N copies before the first expression runs. At 778 symbols and 523,779
rows the pool stopped producing factors entirely: the parent sat at 0 CPU
seconds accumulated while its spawned workers stayed frozen. `max_workers=1`
computes serially, pays the serialisation once, and finishes. Both scripts now
use it, and `template.py` documents why the option is real.

## The gates the run has not cleared yet

ICIR 0.369 against a target of 0.50 is the remaining signal-quality gate, and it
is the one the evidence says will not move through ensembling or scaling. The
trading gates - sharpe 1.14, commission 0.3 percent of capital - clear at
rb20.

Nothing here should be graded as ready for the paper path until the ICIR gate
clears on a window the model never saw, because a signal whose daily IC half
vanishes in a different dispersion regime is not a signal a live book can size
against.
