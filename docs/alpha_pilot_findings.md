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

## ICIR resisted the noise-reduction attempts

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

## What the horizon knob actually does

``alpha_gate.py --hold-days N`` rehorizons without recomputing the 158 factors,
because ``result_df`` keeps the raw ``close`` and the horizon is one expression
over it. The label is written into ``result_df``, ``raw_df``, ``infer_df`` and
``learn_df`` together - ``LgbModel`` trains off ``learn_df`` and ``fetch_infer``
scores off ``infer_df``, so changing one without the others would fit on one
horizon and score on another.

While building that, the expression ``ts_delay(close, -5) / ts_delay(close, -1)
- 1`` was suspected of being backward-looking, which would have meant every IC
on this page was measuring momentum rather than prediction. It is not: the
negative window shifts the series forward, and against the realised forward
return the cached label correlates at 0.9046, while a hand-computed six-bar
frame matches the expression digit for digit. The label is the return from the
next tradeable session to the horizon, which is the window a T+1 market lets a
book capture.

The horizon does not behave the way a longer holding period would, because the
denominator moves with the numerator: at ``hold_days=n`` the expression compares
close at n sessions ahead against close at one session ahead, so the measured
window widens by a single bar rather than by n. Two horizons on the same
features and the same clean split:

| hold_days | IC mean | ICIR | IC positive |
| --- | --- | --- | --- |
| 5 | 0.0520 | 0.349 | 0.632 |
| 10 | 0.0429 | 0.294 | 0.599 |

Ten is worse than five on every axis. Widening the measurement window by one
bar does not buy a better signal, which is the same verdict the seed ensemble
and the feature normalisation already received from different directions. A
genuine longer-horizon signal would need the denominator pinned at the entry
bar instead, and that is a change to the label rather than to its window.

## What ICIR 0.5 would actually take

At the measured IC mean of 0.0520 and ICIR 0.349, the daily IC has a standard
deviation of 0.1489. Reaching 0.5 means one of two things:

* hold the mean and cut the standard deviation by 30 percent, from 0.1489 to
  at most 0.1039, or
* hold the standard deviation and raise the mean from 0.0520 to 0.0745, which
  is 2.4 times today's level.

Neither is available from the levers this repository has already tried. The seed
ensemble bought 0.019 of ICIR. The feature normalisation lost IC outright. The
longer horizon lost both. The dispersion is not estimator noise - the label's own
cross-sectional standard deviation is 5.64 percent a session over 617 symbols, so
the daily IC swings with the market and not with the model.

Given the label IC is already positive every year (2024 0.0687, 2025 0.0408,
2026 0.0454), the honest reading is that a 0.5 target of 0.04 mean and 0.5 ICIR
are asking for different quantities from the same signal: the mean is a
level question, which 778 symbols already clears, and the ICIR is a stability
question, which needs a signal that does not decay when the market's dispersion
changes. A different factor set, not a better-tuned one. This goal is not
complete because of that, not because the feature integration is unfinished. The
trading gates do clear, and they clear by a lot:

| gate | target | measured at rb20 mt0.15 | verdict |
| --- | --- | --- | --- |
| sharpe | >= 1.0 | 1.141 | clears |
| commission / capital | <= 0.02 | 0.0028 | clears |
| rank IC mean | >= 0.04 | 0.0520 | clears |
| IC positive share | >= 0.55 | 0.632 | clears |
| ICIR | >= 0.50 | 0.349 | short |

The remaining work is to raise the signal's own stability, and the two ways that
have not been tried are a factor set built for a longer horizon (a volatility or
fundamental set rather than another reweighting of Alpha158's price-and-volume
candlesticks) and a universe that keeps its size when the market narrows. Both
need a data source this repository does not fetch yet, which is the next thing to
build rather than a parameter to set.

## Refitting on a moving window trades IC for stability

A single fit scored over two years decays. The fitted IC falls from 0.0687 in
2024 to 0.0408 in 2025 on the very same signal, which is a stale model rather
than a market that stopped paying. ``alpha_gate.py --rolling TRAIN STEP``
refits every ``STEP`` sessions on the most recent ``TRAIN`` sessions and
concatenates the out-of-sample predictions, laid out on the dataset's own
trading calendar.

The window stops ``horizon`` sessions short of the block it scores, because the
label reaches ``horizon`` sessions forward: training on a session whose label
reaches into the scoring block would fit the model on ground truth it is about
to be graded against.

Four windows on the same features and the same clean split:

| rolling (train, step) | IC mean | ICIR | IC positive |
| --- | --- | --- | --- |
| single fit | 0.0520 | 0.349 | 0.632 |
| 240, 60 | 0.0264 | 0.318 | 0.651 |
| 500, 120 | 0.0344 | 0.423 | 0.666 |
| 900, 250 | 0.0419 | 0.485 | 0.705 |
| 1400, 300 | 0.0247 | 0.527 | 0.667 |

The pattern is real and it is not free. A short training window makes the model
worse than not refitting at all (240, 60 loses on both axes), and ICIR improves
monotonically as the window grows - 0.318, 0.423, 0.485, 0.527 - until at 1400
sessions ICIR clears 0.50 while IC mean falls back to 0.0247 and fails its own
gate. So the two gates pull against each other: refitting stabilises the daily
IC, and it does so by weakening the signal the recency brought in.

The best cell is 900, 250, which misses only ICIR, by 0.015, and is the only
configuration that clears IC mean, IC positive share and gets ICIR within
touching distance at the same time. That is the closest this signal has come to
clearing all five gates, and the reason it does not is that IC and ICIR are
being asked for by the same 158 price-and-volume factors rather than by a
signal that is strong on both.

## The rolling signal holds the trading gates, and improves two of them

``alpha_pilot.py --rolling TRAIN STEP`` drives the backtest off the same rolling
signal the gate grades, imported from ``alpha_gate`` rather than reimplemented,
so the IC that was measured and the signal that was traded are one thing.

At rb20 with a 0.15 turnover cap, on 778 symbols over the clean split:

| | return% | sharpe | drawdown% | turnover | commission% |
| --- | --- | --- | --- | --- | --- |
| single fit | 5.27 | 1.141 | -1.52 | 2.85 | 0.28 |
| rolling 900, 250 | 5.27 | 1.146 | -1.26 | 2.22 | 0.22 |

The return is unchanged and every risk figure improves, which is what a genuinely
fresher model looks like rather than a fitted one. Against the five gates the
rolling configuration clears IC mean (0.0419), IC positive share (0.705),
sharpe (1.15), and commission share (0.0022), and misses only ICIR at 0.485
against 0.50.

That is one number away from the whole set, and the two ways to reach it that
have not worked are worth naming so they are not tried again: averaging three
seeds over the rolling fit moved ICIR from 0.485 to 0.461 and IC positive share
the wrong way, and rehorizoning the rolling label (hold_days 3 and 8) produced
0.180 and 0.399. ICIR is the gate that remains, and it is a property of the
signal rather than of the book that trades it.

## The market-sized run, and what the first scan got wrong

On the full 3021-symbol dataset with a rolling 900/250 fit, the gate reports
rank IC 0.0456, ICIR 0.457 and positive share 0.697 over 501 sessions. Two of
the three signal gates clear; ICIR is short by 0.043. The earlier numbers in
this file that showed IC 0.0333 and ICIR 0.355 came from a run that passed
``--hold-days 5`` to a dataset whose cached label already matched that
horizon, which rewrote the label in four frames and left the fitted horizon
disagreeing with the scored one. The cached label was checked against a fresh
recomputation (mismatch 0.0), and the runs without ``--hold-days`` are the
ones to trust.

The first market slice scan was wrong in two ways, and both were silent:

* it ranked symbols by the turnover of the last 250 cached bars, which is
  November 2026, and then applied that ranking to sessions from 2024; and
* it scored each liquidity bucket on the sessions where that bucket happened
  to trade, while scoring the full market on every session, so the two
  numbers were not comparable.

The rewrite measures turnover over a trailing 60-session window, shifts it by
one session so the value attached to a session is built only from earlier
sessions, and scores every bucket on exactly the dates the reference is
scored on. ``tests/test_alpha_universe_scan.py`` pins both.

## IC and the money disagree, and the difference is not a measurement error

With the corrected scan, the full market reads IC 0.0456 and the liquidity
buckets read 0.0396 to 0.0488, so liquidity is not where the edge is or is
not. What the scan also found is that the signal's own quintile split is
monotonic but tiny, and that the top of the ranking is the worst place to
trade it:

| signal quintile | 1 (strongest) | 2 | 3 | 4 | 5 (weakest) |
| --- | --- | --- | --- | --- | --- |
| mean label, 5 sessions | 0.44% | 0.34% | 0.33% | 0.28% | 0.03% |
| mean label with the day's mean removed | 0.16% | 0.05% | 0.05% | -0.00% | -0.26% |

The strongest quintile beats the weakest by 0.42 percent over five sessions.
That is the whole edge: about two percent a year before costs, and it is
monotonic, so it is real. But the rank IC of 0.0456 on 3000 names is not
measuring that 0.42 percent in a way a book can use.

Taking the strongest 25 percent of each liquidity bucket and scoring it
gives IC -0.0169. The controls say why: a random signal through the same
slice gives -0.0003, the real signal with the buckets shuffled gives -0.0157,
the label shifted one session gives 0.0013 and the label lagged one session
gives 0.0008. The slice is not finding a hidden market; it is finding the
bottom of the ranking, where the model's least confident calls sit and where
the measured correlation turns negative. The earlier report that the same
slice produced IC 0.1032 was a direction bug: ``rank`` numbers the smallest
value 1, so the slice kept the model's worst calls, and a negative
correlation between those and the label reads as a positive one once the
sign is flipped.

## The signal's edge lives in a handful of stocks, and the median stock loses

``scripts/alpha_portfolio_probe.py`` measures what a book could have earned
from the same cached signal, buying at the session after the signal is
scored, which is the first session a book can actually trade. The forward
return starts one session later on purpose: a model that has seen session
T's close cannot buy at that close.

The quintile split reproduces the dataset's own label to four decimals, so
the probe and the gate are looking at the same thing. The portfolio numbers
then say something the IC does not:

| slice | mean 5-session return | median | best stock's average |
| --- | --- | --- | --- |
| top 30 of 2893 | 0.84% | -0.49% | 29.5% |
| top 100 | 0.66% | -0.39% | 36.7% |
| top 600 | 0.44% | -0.24% | 46.5% |

Every slice has a negative median. The mean is carried by a handful of
stocks that moved 30 to 45 percent inside five sessions, and the more of
them a slice holds the larger that average becomes. This is the shape of the
strategy a book cannot run: the median holding loses, and the return depends
on catching a small number of jumps that are not knowable in advance.

The rank band makes the same point. On the second half of the out-of-sample
window (2025-07 onwards), the strongest 1 percent has a median of -0.82
percent and the 10 to 20 percent band has -0.31 percent, while the weakest
half of the ranking has a mean of 0.009 percent, which is nothing. The
ranking is still informative - every band beats the weakest one - but the
money is in the tail and the tail is where the selection noise is.

## The signal decays after 2025-07, and that is what ICIR is measuring

Splitting the window in half shows the decay. On the first half every slice
earns a positive mean; on the second half the top 30 earn 0.51 percent per
five sessions instead of 1.43, the top 100 earn 0.29 instead of 1.35, and
the weakest half of the ranking earns nothing at all. The gate's ICIR of
0.457 against a target of 0.50 is the daily IC swinging with that decay, not
an estimator that can be averaged away.

The rolling window is the obvious suspect: a 900-session window that scores
2026 holds about two years of data from 2024 and 2025, and the signal those
years produced is weaker than what it is being asked to predict.

## The year and the rolling block say which half is the problem

The daily IC by year, on the same 900/250 rolling fit:

| year | rank IC | IC std | ICIR | sessions |
| --- | --- | --- | --- | --- |
| 2024 | 0.0529 | 0.1124 | 0.470 | 64 |
| 2025 | 0.0412 | 0.1017 | 0.405 | 243 |
| 2026 | 0.0487 | 0.0932 | 0.522 | 194 |

2026 clears the ICIR gate on its own. 2025 carries 243 of the 501 sessions
and is the one that drags the ratio down. The signal is not gone in 2025:
the quintile spread that year is 0.308 percent, positive but half what 2024
paid, and in 2026 every quintile is negative while the spread is still
0.524 percent - that year the market fell and the ranking still separated
the names.

Splitting by rolling block rather than by year is what identifies the cause.
Each block is scored by one model fitted on the window before it:

| block | rank IC | ICIR |
| --- | --- | --- |
| 2024-09-26 to 2025-10-13 | 0.0461 | 0.419 |
| 2025-10-14 to 2026-10-23 | 0.0447 | 0.503 |

The second model, trained on data that includes the weak 2025, clears the
ICIR gate. The first one, which was fitted before 2025 and spent the whole
of 2025 being scored, does not. The stability of this signal is therefore
not a property of the factors; it is a property of how long the book waits
before refitting. A step of 250 sessions leaves a model in place for most of
a year, and one bad year inside that year is enough to pull the whole ratio
below the gate.

## What was tried and did not work

Four routes were measured and rejected, so they are not tried again:

* **A liquidity filter.** The corrected as-of buckets read 0.0396 to 0.0488
  across five buckets, with no bucket clearly better, and slicing the
  ranking by liquidity changes nothing.
* **Taking the strongest names.** The strongest 25 percent of each bucket
  scores IC -0.0169, the same slice on a random signal scores -0.0003, and
  the label moved one session forward or backward scores 0.0013 and 0.0008.
  The signal's ordering information does not survive being cut down to the
  names the model likes most.
* **A shorter training window.** 500/120 reads IC 0.0388 and ICIR 0.438,
  below the 900/250 fit, and the quintile spread falls from 0.42 to 0.32
  percent. The window is not too long.
* **Smoothing the signal over time.** Averaging the prediction over 2, 3,
  5, 10 and 20 sessions moves ICIR from 0.465 down to 0.452, 0.430, 0.394,
  0.394 and 0.359. A five-session label already carries the signal's own
  inertia, and smoothing on top of it only adds lag.

## Regularising the booster changes nothing, and the factor set makes it worse

The rolling fit points the train segment and the validate segment at the
same window, so the validation loss is the training loss and early stopping
can never fire: every fit runs the full thousand rounds. On 2.4 million rows
and 158 factors that looked like overfitting worth fixing, so
``LgbModel`` grew regularisation arguments and they were measured on the
window the rolling fit uses and the sessions that follow it:

| settings | rank IC | ICIR | positive share |
| --- | --- | --- | --- |
| baseline | 0.0452 | 0.503 | 0.708 |
| 15 leaves | 0.0481 | 0.480 | 0.695 |
| 1000 rows per leaf | 0.0410 | 0.492 | 0.708 |
| half the features | 0.0420 | 0.453 | 0.686 |
| leaves, rows, features and L2 together | 0.0449 | 0.449 | 0.686 |

Nothing beats the baseline. The booster's flexibility is not what makes the
daily IC wobble.

The other untried factor set is worse. Built on the same 778 symbols, with
the same rolling fit and the same label:

| factor set | rank IC | ICIR | positive share | quintile spread |
| --- | --- | --- | --- | --- |
| Alpha158 | 0.0412 | 0.479 | 0.701 | 0.40% |
| Alpha101 | 0.0319 | 0.343 | 0.621 | 0.26% |

The WorldQuant 101 factors are not the answer on this data.

## The market-sized cross-section is what costs the ICIR

The same Alpha158 model on the same dates, at two sizes:

| universe | rank IC | ICIR | 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- |
| 778 symbols | 0.0412 | 0.479 | 0.499 | 0.464 | 0.491 |
| 3021 symbols | 0.0456 | 0.457 | 0.470 | 0.405 | 0.522 |

The wider universe raises the IC, which is what widening was meant to do,
and lowers the ICIR, because a daily ranking across 3000 names is harder to
keep stable than a ranking across 778. On 778 symbols the signal clears
0.5 in two of the three years; on 3021 it clears it in one.

That reframes the goal. The book does not have to hold the 30 strongest names
out of 3000 to capture the edge - the portfolio probe already showed that
the strongest names carry a negative median and that the mean is carried by
a handful of jumps. A wider book over the same ranking should be steadier
than a narrow one, because the names that make the narrow book's return are
the same names that make its variance.

## The rebalance clock has to match the label, and the rest is noise

The backtest on the market-sized set, holding 30 names and rebalancing every
20 sessions, fails the trading gates: 1.40 percent total return, a 0.78
sharpe, and 142 trades over 515 sessions. The clock, not the signal, is what
moved. A five-session label is a prediction about the next five sessions, so
a book that rebalances every twenty sessions is trading a horizon it did not
ask about, and the 778-symbol rb20 numbers in this file do not carry over to
the wider universe.

Matching the clock to the label raises the sharpe from 0.78 to 1.29, and the
sweep over the market-sized set reads:

| top_k | rebalance_days | sharpe | annual return | commission |
| --- | --- | --- | --- | --- |
| 30 | 3 | 0.43 | 0.74% | 0.86% |
| 30 | 5 | 1.29 | 2.80% | 0.53% |
| 30 | 10 | 0.42 | 0.68% | 0.27% |
| 50 | 3 | 0.70 | 1.01% | 0.82% |
| 50 | 5 | 1.52 | 2.49% | 0.52% |
| 50 | 10 | 0.74 | 1.00% | 0.28% |
| 100 | 5 | 1.35 | 1.67% | 0.49% |
| 100 | 10 | 1.05 | 1.16% | 0.26% |
| 200 | 5 | 1.15 | 1.12% | 0.45% |
| 200 | 10 | 1.02 | 0.98% | 0.26% |

The sharpe at rebalance_days 5 stands out, and it should not be trusted on
its own. Holding top_k 50 and sweeping the clock one session at a time gives
4 days 0.08, 5 days 1.52, 6 days 1.04, 7 days 0.38, 8 days 1.08. That is not
a peak, it is dispersion. The cells that survive their own neighbours are the
5 to 10 session clocks at 100 to 200 names, which sit between 1.02 and 1.35
with commissions under half a percent. Those are the settings that would be
quoted as the trading gates, and the 1.52 is a number this file records as
measured and does not rely on.

## The ICIR ceiling, and where it comes from

Fourteen ways of moving ICIR were measured and none of them crossed 0.50 on
the market-sized cross-section:

| direction | rank IC | ICIR |
| --- | --- | --- |
| rolling 900/250, one seed | 0.0456 | 0.457 |
| rolling 500/120 | 0.0388 | 0.438 |
| rolling 900/120 | 0.0465 | 0.476 |
| rolling 900/250, seeds 42+1+2 | 0.0492 | 0.461 |
| signal smoothed over 5 sessions | 0.0459 | 0.394 |
| signal smoothed over 20 sessions | 0.0478 | 0.359 |
| 15 leaves | 0.0481 | 0.480 |
| 1000 rows per leaf | 0.0410 | 0.492 |
| half the features per tree | 0.0420 | 0.453 |
| leaves, rows, features and L2 together | 0.0449 | 0.449 |
| relabelled to 10 sessions | 0.0436 | 0.430 |
| Alpha101 instead of Alpha158, 778 symbols | 0.0319 | 0.343 |
| the two factor sets ensembled | 0.0453 | 0.444 |
| liquidity buckets, all five | 0.0396-0.0488 | 0.358-0.449 |

The 1000-rows-per-leaf run lands at 0.492, which is the closest, and it is
also the one that lost the most IC mean: it is buying the ratio with the
level, which is the same trade the 1400/300 window made on 778 symbols.

What the evidence supports is that the ratio is a property of the
cross-section. On 778 symbols the same model reads ICIR 0.479 with the
per-year split 0.499, 0.464, 0.491 - close to the gate in every year. On
3021 symbols it reads 0.457 with the split 0.470, 0.405, 0.522. The wider
universe raises the IC, which is what it was built for, and the daily ranking
of 3000 names is harder to keep stable than the ranking of 778. Those are the
same two effects the widening was supposed to separate, and on this data they
move together.

So the goal's ICIR gate, on this factor set with this data source, is not
reachable by changing how the same 158 price-and-volume factors are fitted.
The gate that has not been tried is a different information set - a size,
valuation or fundamental one - and the data source this repository fetches
(`push2his`) carries only OHLCV bars, so that is a data problem before it is
a modelling one.

## The size stratum is the one thing the data can still say, and it says no

The cheapest version of a different information set is a size stratum. The
cached bars carry no market cap, no valuation and no fundamentals, but
turnover is volume times price, and the trailing mean of it says how heavily
a name trades. ``scripts/alpha_size_strata.py`` cuts the market into three
strata on the as-of rank of that trailing turnover, so a name's stratum on
session T is built only from sessions before T, and scores every stratum on
the same sessions the full market is scored on.

The result on the 3021-name cross-section, 501 out-of-sample sessions:

| stratum | rank IC | ICIR | positive share | ICIR of a random signal |
| --- | --- | --- | --- | --- |
| whole market | 0.0456 | 0.457 | 0.697 | - |
| largest by turnover | 0.0462 | 0.391 | 0.653 | 0.017 |
| middle by turnover | 0.0494 | 0.468 | 0.704 | -0.006 |
| smallest by turnover | 0.0400 | 0.371 | 0.654 | -0.026 |

The random-signal control is what makes this readable: it sits within 0.03
of zero in every stratum, so the signal inside a stratum is real, and the
stratum's ICIR is not being propped up by the slice.

The middle stratum is the best of the three and it still misses the gate at
0.468, and the split is not stable either:

| stratum | 2024 | 2025 | 2026 |
| --- | --- | --- | --- |
| largest | 0.255 | 0.522 | 0.310 |
| middle | 0.422 | 0.388 | 0.609 |
| smallest | 0.576 | 0.232 | 0.508 |

Every stratum falls below 0.5 in at least one year, and the smallest one
swings from 0.576 to 0.232. There is no year in which all three clear the
gate, and no stratum that clears it in two years out of three.

The direction of the movement is the tell. Every stratum reads a *lower*
ICIR than the whole market even though the middle one reads a higher IC.
That is not the signal degrading inside a slice; it is the slice shrinking the
cross-section, which shrinks the dispersion of the daily IC series and so
changes what a mean-over-standard-deviation ratio means. A narrower book over
the same ranking is not automatically a steadier signal, and the numbers say
it is not one here.

So the fifteenth direction is measured and rejected as well, and the
conclusion it reaches is the same one the other fourteen reached: on this
data source the ICIR gate is not reachable by slicing the market. A real
size, valuation or fundamental series would be a genuinely different
information set, and this repository does not have one.

## The scans were refitting a model they already had

Two bugs sat in the cache lookup that every scan shares, and together they
made each scan cost about seven minutes of LightGBM fitting it never needed.

The first was in the filename. The cache key always carries the seed list,
because a signal fitted with one seed is not the signal fitted with another,
but the lookup compared the caller's filename against that key. The default
filename has no seed suffix in it, so the comparison could never succeed.

The second was in the metadata. The label is written into the parquet's
custom metadata, and the code read it through ``DataFrame.schema``, which
carries the column types and nothing else. The comparison was therefore
between the dataset's label and an empty string, which failed, and the scan
refitted.

Both are now covered by tests, and a scan that used to take seven minutes
takes twenty-eight seconds. The numbers did not change when the bugs were
fixed - the same cached fit reads the same IC 0.0456 and ICIR 0.457 - which
is the check that the fixes changed how the work was found and not what the
work found.

### How the host has to be driven

The kline host's refusal is soft, and driving it well was the difference
between a fetch that finished and one that did not. Measured on batches of
sixty distinct symbols through the jsnzkpg4 gateway:

| workers | gap per worker | answered | rate | 2340 pending at that rate |
| --- | --- | --- | --- | --- |
| 8 | 5.0s | 50/60 | 1.06/s | 37 min |
| 12 | 3.0s | 44/60 | 1.63/s | 24 min |
| 16 | 2.0s | 37/60 | 1.23/s | 32 min |
| 24 | 1.5s | 27/60 | 0.94/s | 42 min |

Twelve workers with a three second gap each is the winner, and the thing
that makes it work is that the gap is **per worker**. One shared gap caps
the whole fetch at one request per gap no matter how many workers are
running: eight workers sharing a five second gap measured 0.067 symbols a
second, because the eight of them were queueing on one clock and most of the
time was spent waiting in it.

The success rate also has a floor of its own. A single batch of distinct
symbols at a gap of a second and a half got 20 of 30 through, and the same
batch after a fifteen second pause got 28 of 30, so the failures are the
host being hit and not the symbols being wrong. The fetcher therefore keeps
the symbols that did not land and tries them again in later passes, with a
twenty second pause between passes.

Which gateway works is not a property of the machine. Measured against a
host that was refusing every direct request, the jsnzkpg4 gateway answered
8 of 8 while the direct host answered 0 of 8 and the seep gateway answered
0 of 8. The fetch tries jsnzkpg4 first and falls back to the others.

## The size information set was already in the data

The section above closes with the reason the gate has not been reached: the
factor set is price and volume, the ranking of 3000 names is harder to keep
stable than the ranking of 778, and the direction that has not been tried is
a different information set. It then says that a size, valuation or
fundamental series would be a genuinely different information set, and that
the data source this repository fetches carries only OHLCV bars, so that is
a data problem before it is a modelling one.

That last sentence was wrong about the host. It was right about the
request. The daily kline endpoint the repository already calls was being
asked for seven fields:

```
fields2=f51,f52,f53,f54,f55,f56,f57
```

and it will answer with more. Field f61 is the turnover rate in percent and
field f70 is the float share count, published per session, so one extra
field on the same request carries a size series that the pipeline was
missing. No new host, no new dependency and no new crawl: the size
information set was in the response all along and the request had left it
on the floor.

### What f70 actually is

Two readings agree, and neither of them is a guess.

The first is arithmetic. Eastmoney reports volume in lots, so the float
count implied by the turnover rate is ``volume * 100 / (turnover / 100)``,
and that count divided by f70 is:

| name | implied / f70 |
| --- | --- |
| Eastmoney | 1.0022 |
| BYD | 0.9992 |
| Moutai | 1.0050 |
| ICBC | 0.9906 |

It is one, so f70 is the float share count and not the total share count.
The market caps it produces are the right order of magnitude for the
market: 1.89 trillion yuan for Moutai and 1.02 trillion for ICBC.

The second is that the series is point in time rather than backfilled. SMIC
went from 1.04 billion shares on 2020-07-16 to 1.10 billion on 2021-01-18
to 1.87 billion on 2021-07-16, and CATL went from 2.03 billion to 2.31
billion and back inside 2021. Every move lands on a date the capital
actually changed and the value is flat between those dates, so a factor
built on it cannot see a share issue before it happened.

The two readings together are what make this usable: f70 is float shares,
it is known on the day, and multiplying it by the session's price gives the
float market cap.

### The price in the dataset cannot be used for this

The lab normalises every price by the first close it sees, so a normalised
close of 0.99 means something different for a 100-yuan stock and a 5-yuan
one. A market cap built on it would be the cap of the wrong company.

The market cap is therefore built from turnover over volume, which is the
session's average traded price in yuan, so the cap comes out in yuan
without needing an unadjusted close:

```
price_filled = forward_fill(turnover / volume) over the symbol
float_cap    = price_filled * float_shares
```

The fill is over the symbol and not across it, so a suspended session
carries the last price it traded at and does not leak another name's.

### What the size factors are

The model gets the size itself rather than the market being sliced around
it. A size stratum and a size factor are different things: the stratum
shrinks the cross-section, which is what lowered ICIR in every slice above,
while the factor leaves the cross-section whole and gives the booster
something the 158 price factors cannot carry.

| factor | what it is |
| --- | --- |
| size_log_cap | log of the float market cap |
| size_cap_pct | the name's size rank inside its own session |
| size_relative | its log cap against the session's mean log cap |
| size_turnover | turnover against its own cap |
| size_cap_change_5/20/60 | the cap's move over each window |
| size_share_change_5/20/60 | the share count's move, which separates dilution from a falling price |

Every window is trailing, so a factor on session T only sees sessions up to
T, and the size on session T is the share count the host published on
session T.

### How the host behaves

The kline host drops the connection outright when it is hit in bursts, and
the block outlasts a short pause. A single-threaded run at one and a half
seconds per request got through 146 symbols before the host started
refusing, and the refusals were not code-specific: they were the symbols
that happened to fall inside the blocked window. Four workers sharing one
request gap got through 15 of 20 while the block was still clearing.

So the fetcher runs four workers against a shared gap, retries the symbols
that did not land in further passes, and waits between passes. The cache is
per symbol and a symbol that already landed is never fetched twice.

### What the first read says

The pilot ran while the fetch was still going, so it answers the narrower
question on the 355 names that had landed rather than the gate on all of
them. The same rolling 900/250 fit, the same seed, the same sessions, and the
only difference is whether the booster can see the size:

| dataset | names | rank IC | ICIR | positive share |
| --- | --- | --- | --- | --- |
| price factors only | 355 | 0.0292 | 0.308 | 0.613 |
| price and size factors | 355 | 0.0333 | 0.355 | 0.633 |
| random signal, same names | 355 | -0.0015 | -0.030 | 0.464 |

Both numbers move up, and the control says the signal inside them is real.
That is the first time in this file that an addition to the factor set moves
IC on out-of-sample sessions: the fifteen directions before it all moved how
the same 158 price factors are fitted, and none of them crossed 0.50.

The reading is not a gate result and is not being presented as one. A subset
has a narrower cross-section, and a narrower cross-section reads a different
ICIR for reasons that have nothing to do with the factors: the size-stratum
section above is where that effect was measured directly. The price-only
number here, 0.308 on 355 names, is not the 0.457 the gate reads on 3021 -
which is the cross-section doing what the stratum section said it would do,
and not a regression in the signal.

So the honest statement is that the size information set carries
information the price factors do not, measured on the names covered so far,
and whether it moves the gate on the whole market is what the run on the
full cross-section will say.

What has not been measured is the gate on the whole market. The baseline that
the gate is scored against reads 0.457 on 3021 names, and this run is on 355.
The size dataset built on the full fetch is what the gate will be measured
against, and until that run exists the gate still reads 0.457.
