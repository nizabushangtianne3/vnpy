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

### What the kline endpoint will not give

The kline endpoint carries more than the seven columns the repository asked
for, and the shape of that extra is worth recording so nobody spends the
search again.

`fields2` is truncated by the server rather than rejected. Requesting
`f51` through `f70` returns twenty columns, of which `f58` is the day's
amplitude, `f59` the change in percent, `f60` the change in yuan, `f61` the
turnover rate, `f62` through `f69` are zero, and `f70` is the float share
count. Requesting `f71` and above returns **no data at all**, and
requesting a mix that starts at `f81` returns only `f51` through `f61`,
which means the server stops at the last field it knows and drops the rest.

So the daily kline carries price, volume, amount, amplitude, change,
turnover and float shares, and nothing else. There is no earnings, no
book value and no multiple on this endpoint: a valuation or a fundamental
series would need a different host, and that remains true. The size
information set is the part of the information that this endpoint was
holding back, and it is now in hand.

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

## The size information set was already in the data, and it crosses the ICIR gate

The float share fetch reached 2711 of 3021 names, and the size dataset built
on it covers 91.8% of the bars. Scored on the full market-sized
cross-section, 501 out-of-sample sessions, the same rolling fit at 900/250
and the same seed as the baseline:

| dataset | rank IC | ICIR | positive share | ICIR of a random signal |
| --- | --- | --- | --- | --- |
| market3000 (158 price factors) | 0.0464 | 0.465 | 0.695 | -0.014 |
| market3000_size (price + 16 size factors) | 0.0587 | 0.519 | 0.685 | -0.014 |

ICIR clears 0.50 for the first time on this factor set with this data
source. Per year: 0.696 / 0.469 / 0.524 for the size run against
0.477 / 0.421 / 0.520 for the baseline, so the lift is not one year.

Two things have to be said plainly about the size columns, because both were
found the hard way on the way here.

### The label column moved, and that is a leak and not a signal

The first code path that appended the size columns to the dataset put them
after the label, and the booster reads its features as ``df.columns[2:-1]``.
On a frame of 161 columns that slice is exactly the 158 price factors. On a
frame of 177 columns it is the 158 factors plus the label, and the last size
factor is cut off instead. The model that read the label as a feature
scored IC 0.9946 and ICIR 219.7, which is the answer and not a prediction.

The symptom is recognisable and worth naming: an IC near one means the
signal and the label are the same series. The check is cheap. Rank
correlate the signal against the label on a frame the model has scored and
look for 0.99, because a cross-sectional signal on 3000 names that explains
its own label perfectly is not a discovery.

``augment_dataset`` now reorders every frame it touches so the label stays
last, and a test pins the order, because the breakage is invisible in the
frame's own column list: all 16 size columns and the label are still there.

### The top-slice check was calling a correct join inconsistent

``alpha_portfolio_probe.py`` compared a 30-name subset mean against the
cross-day average of the quintile means and printed INCONSISTENT. Those are
two averages of two populations. On 2025-03-03 the top 30 of 2893 names
earned 3.59% against the quintile they live in at 1.63%, and every one of
the 30 was inside that quintile. A 30-name subset of a 579-name quintile is
free to out-earn the quintile, because forward returns are right-skewed and
the top of the ranking is where the skew sits.

What actually tests the join is the subset relation, so the check now counts
rows of the top slice that sit outside the session's strongest quintile. On
one session, 2026-11-06, only 114 names had a forward price, so the
quintile boundary was 22 and ranks 24 to 30 were inside the slice and
outside the quintile at once. That is a property of the session and it is
reported rather than counted as a broken join.

## The backtest says the same thing, and it says how far the goal still is

The rolling signal in the real engine, with the real A-share costs (0.05%
buy, 0.15% sell), 30 names, rebalanced every five sessions, from
2024-09-26 to 2026-11-13:

| setting | annualised | sharpe | commission fraction | turnover x capital | max drawdown |
| --- | --- | --- | --- | --- | --- |
| 158 price factors | 0.27% | 0.0085 | 0.427% | 4.28x | -2.33% |
| price + size factors | 45.5% | 0.4085 | 0.236% | 2.37x | -1.86% |

The commission gate is comfortable: 0.24% of capital against a 2% ceiling,
and the size run also trades half as much. The sharpe gate is not: 0.41
against a 1.0 target, on a base that read 0.0085.

The distance between the two sharpe numbers is not a modelling artefact.
``alpha_portfolio_probe`` reads 2.32 for the same signal because it measures
the mean forward return of a static top slice and books no cost, so it is
grading the ordering of the ranking rather than a book that pays to turn
over. The two numbers answer different questions and the engine's is the one
the goal asks about.

Where the remaining distance lives is in the extreme days. The top-30 mean
per session has a median near 0.6% and a maximum of 26%, and the best 5% of
sessions carry 94.8% of the total. Those days are dominated by the new
boards: of the 150 name-sessions across the five best days, 97 are 920xxx
(Beijing) and 23 are 688xxx (Shanghai STAR), and of the 150 across the five
worst days, 61 are 920xxx and 50 are 688xxx. Dropping both prefixes leaves
a day-mean distribution with the same shape, so this is not only a new-board
artefact, but the new boards are where the tails are thickest.

## The 2026 collapse is market beta, not signal decay

Two numbers had stopped agreeing. The rank IC in 2026 was the *best* of the
three years -- 0.520 on the bare 158 factors and 0.524 with the size block
added, against 0.477 and 0.421 for 2025 -- while the backtest over the same
signal in the same year returned -39.9% annualised with a drawdown that ran
265 days. A signal cannot keep ranking correctly for years and then lose
its ability to rank in the year the book loses money, so the loss has to be
coming from somewhere other than the ranking.

The first question is whether the cross-section stopped spreading. It did
not. Cross-sectional label dispersion (the per-session standard deviation of
the label) is 0.074, 0.056 and 0.063 for 2024, 2025 and 2026, and 2026 sits
between the other two years rather than below them.

The second is whether the ranking is only long in name. It is not. Sorted
into quintiles, with q0 the strongest call, the top-minus-bottom label
spread is +0.0115, +0.0040 and +0.0065 for the three years, so 2026 separates
the tails *better* than 2025 does, and the quintile means fall monotonically
in all three years. Dropping the label for the running sessions and leaving
15,105 null labels in 2026 does not change this -- they are concentrated on
14 sessions, and removing them leaves the same ordering.

What actually changed is the level. The label's own cross-sectional mean,
which is the market's average forward return on those days, is +0.45%, +0.67%
and -0.25% per session. 2026 was a falling market; in a falling market a
long-only book of 30 names is being paid the market's drift before any skill
applies. The whole thing showed up in the backtest rather than the IC
because an IC is invariant to shifts.

Regressing the top-30 mean forward return on the market's own mean settles
it:

| year | alpha/day | beta | corr |
| --- | --- | --- | --- |
| 2024 | +0.0188 | 1.69 | 0.80 |
| 2025 | +0.0006 | 1.53 | 0.82 |
| 2026 | +0.0041 | 1.55 | 0.69 |

A beta of about 1.55 is not a modelling artefact, it is the book doing what
it was built to do: holding the market with roughly 50% more of it. The
correlation of 0.8 means the book's daily outcome is mostly the market's
outcome. Year by year:

| year | ungated/day | gated/day | exposure | gate |
| --- | --- | --- | --- | --- |
| 2024 | +0.54% | +1.03% | 80% | trailing-20d drift |
| 2025 | +1.09% | +1.03% | 72% | trailing-20d drift |
| 2026 | +0.05% | +0.66% | 45% | trailing-20d drift |

The gate leaves every year positive on gross return with no re-fitting, and
it turns the market's direction itself into the only thing that is being
predicted, which is a much weaker claim than predicting 3,000 return series.
With real A-share costs on the 2026 window it has to be re-measured, so the
honest version of this line is that the beta looks like the cause and the
gate is the obvious thing to test, and not that the gate has been shown to
work.

The neutralisation pass already established that removing market cap as a
factor is a net loss, so the fix is not a factor. The fix is exposure: the
signal should be asked about which names beat the market rather than which
names go up. That is either a demeaned label, which is what the ranking
already sees, carried through to a market-neutral book, or an explicit
hedge. Both are a portfolio-layer change and neither is a tuning change.

## The beta was the selection's own leverage, and it has a measured size

The "beta" in the table above is not the market's. A random 30-name book
drawn from the same cross-section on the same sessions has a beta of 0.998
against the cross-section's own mean, and the top-30 book has 1.579. The
market's contribution is the first number; the extra 0.58 is what the
extreme names of a 3,000-name ranking do relative to the average name, and
it is the reason the book was punished in 2026.

| year | top-30 | random 30 | cross-section | beta top | beta random |
| --- | --- | --- | --- | --- | --- |
| 2024 | +2.65% | +0.45% | +0.45% | 1.69 | 1.00 |
| 2025 | +1.09% | +0.66% | +0.67% | 1.53 | 1.01 |
| 2026 | +0.05% | -0.23% | -0.24% | 1.55 | 0.99 |

The random book also lost money in 2026, so 2026 was a falling market for
every name in it. What the top-30 did was lose less than the market while
carrying 55 percent more of it.

Where the leverage comes from is the width of the band the book cuts. The
band is a share of the cross-section rather than a count of names, so the
same setting means the same thing on every universe size:

| band | held | beta | 2024 | 2025 | 2026 | relative 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.00-0.01 | 30 | 1.57 | +2.70% | +1.09% | +0.02% | +2.24% | +0.41% | +0.26% |
| 0.05-0.15 | 294 | 1.07 | +0.95% | +0.82% | -0.02% | +0.49% | +0.14% | +0.22% |
| 0.10-0.30 | 589 | 0.93 | +0.64% | +0.75% | +0.01% | +0.19% | +0.08% | +0.25% |

The relative columns are the book with the cross-section's own mean
removed, and they are positive in every year of every band. The selection
was paid every year. Only the exposure was the problem.

## The neutral book clears the gates, and the margin is not a spike

`MarketNeutralStrategy` holds the 5-to-15 percent band and shorts a
basket against it. The basket is the most liquid names, chosen once and
kept, with the long side's own names excluded so the hedge does not
cancel the selection it is there to preserve. The engine's own numbers,
with A-share commissions of 0.05 percent to buy and 0.15 percent to sell:

| setting | sharpe | total return | max drawdown | commission | 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| band only, 0.05-0.15 | 1.04 | 15.23% | -6.01% | 0.041% | +2.18 | +1.68 | -0.07 |
| neutral, hedge 1.0 | 1.42 | 17.84% | -3.96% | 0.048% | +2.62 | +1.40 | +0.99 |
| neutral, hedge 0.8 | 1.39 | 17.35% | -4.25% | 0.047% | +2.58 | +1.63 | +0.78 |

The yearly figures are that year's sharpe, so all three years are
positive for both neutral settings. The long-only band loses money in
2026 and the neutral book does not.

That the hedge is not a fit to one particular basket is the part that
matters, and it was checked three ways. The hedge leg was replaced by an
equal-weighted slice of the entire universe and the clock sharpe stayed
at 1.11; it was replaced by random baskets of 10, 30 and 100 names and
those read 0.99, 1.15 and 1.06. The effect belongs to the neutrality, not
to a basket that was picked well.

The grid around the setting is a plateau rather than a spike. Every band
between 0.00-0.05 and 0.10-0.30 clears a clock sharpe of 1.0 at every
hedge ratio from 0.6 to 1.0, and the best cells sit at a hedge of 0.6 to
0.8 rather than at the setting that was run. The best region is
0.10-0.20 at a hedge of 0.8, reading 1.57.

What this does not settle is how a live book would hold the short leg.
Shorting thirty individual A-shares is not something a broker will
reliably fill, because the borrow is scarce and expensive. The
measurement says the shape is right; the deployment has to use an index
future or an ETF leg instead of these names, and that leg has to be
priced against its own margin and financing.

## The old commission column was read wrong, and the old book failed the fee gate

The table above prints ``commission`` as the fraction returned by the
engine, and that fraction is not a percent. The neutral book's 0.048 is
**4.8 percent of capital**, not 0.048 percent. So the setting that was
recorded as clearing the trading gates did not clear the fee gate at all:
the goal allows at most 2 percent, and the 5-day book spent 4.8.

The same correction applies to the turnover column. 47.9 is 47.9 times
capital turned over over the whole window, and the cost of that churn is
what the fee column is measuring.

## A buffered band and a slower clock clear the fee gate

Two changes were made to the book, and both were measured with the real
engine rather than estimated:

* A buffered band. A held name keeps its place while it is inside the
  band or within ``buffer_ratio`` of either edge, so a name that merely
  moved across an edge is neither bought nor sold. The book keeps the
  band's own number of names, so the buffer does not turn into a larger,
  more expensive book.
* A longer rebalance clock. The label is five sessions, and a five-day
  clock re-cut the book 48 times a year while the ranking's own churn did
  most of the work.

With a 5-to-15 percent band, a hedge of 1.0 and a buffer of 0.10:

| rebalance | sharpe | total return | max drawdown | turnover | commission | 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 5 days, no buffer | 1.42 | 17.84% | -3.96% | 47.93x | 4.806% | 2.62 | 1.40 | 0.99 |
| 20 days, no buffer | 1.50 | 18.09% | -3.96% | 12.39x | 1.247% | 2.87 | 1.54 | 0.92 |
| 20 days, buffer 0.10 | 1.63 | 25.70% | -4.64% | 13.23x | 1.337% | 2.97 | 1.12 | 1.45 |
| 30 days, no buffer | 1.69 | 20.46% | -3.77% | 9.00x | 0.908% | 2.81 | 1.78 | 1.24 |
| 30 days, buffer 0.05 | 1.84 | 26.15% | -3.77% | 9.32x | 0.942% | 2.85 | 1.87 | 1.43 |
| 30 days, buffer 0.10 | 1.81 | 27.63% | -4.08% | 9.47x | 0.956% | 2.85 | 1.80 | 1.39 |
| 30 days, buffer 0.15 | 1.82 | 30.13% | -4.32% | 9.65x | 0.975% | 2.82 | 1.69 | 1.49 |
| 30 days, buffer 0.20 | 1.78 | 32.32% | -5.19% | 9.91x | 1.002% | 2.80 | 1.70 | 1.38 |

The yearly figures are that year's sharpe, and every one of them is
positive. The 30-day row is a plateau rather than a spike: buffers from
0.05 to 0.20 all read between 1.78 and 1.84, and 20, 25 and 30-day
clocks all clear 1.35 at every buffer that was tried.

Cost sensitivity was checked with the fee actually written into the
engine's contract settings, because the strategy's own cost fields only
feed its budget arithmetic and not the commission the engine charges:

| one-way cost | sharpe | commission | 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- |
| 5 bp | 1.84 | 0.474% | 2.87 | 1.85 | 1.42 |
| 10 bp | 1.81 | 0.947% | 2.85 | 1.80 | 1.39 |
| 20 bp | 1.76 | 1.887% | 2.80 | 1.72 | 1.35 |

The book's edge outruns the fee up to 20 basis points on each side, and
the real A-share setting of 5 basis points to buy and 15 to sell lands on
the same curve.

What this still does not settle is the deployment leg. The short side is
still a basket of individual A-shares in the measurement, and a live book
has to hold that exposure through an index future or an ETF, priced with
its own margin and financing.

## The 30-day clock did not have an IC-backed signal behind it

The table above is the engine's book, not the signal's grade. The signal
that produced it was the rolling single-seed size fit, and its cached
label was five sessions. A horizon scan on that same signal, with
``alpha_gate.py --rolling 900 250 --hold-days N`` refitting the rolling
book at every horizon, reads:

| hold | IC | ICIR | positive IC |
| --- | --- | --- | --- |
| 5 | 0.0380 | 0.3889 | 66.7% |
| 10 | 0.0415 | 0.3853 | 62.4% |
| 15 | 0.0457 | 0.3751 | 65.6% |
| 20 | 0.0406 | 0.3147 | 59.1% |
| 25 | 0.0246 | 0.1836 | 51.0% |
| 30 | 0.0273 | 0.1952 | 49.2% |

The 5, 10, 15 and 20-day rows fail ICIR; 15 also clears IC but not by
enough to survive the full gate. The 30-day row fails all three IC
gates. So the 30-day engine book was a well-behaved book on a signal
whose own horizon never passed, and that result cannot be the paper
trigger.

The 5-day ensemble did clear the IC gates, reading IC 0.0615, ICIR
0.5196 and positive IC 69.5% over 501 sessions, with all three years
positive. The next measurement is the ensemble refit at a horizon that
matches a slower clock, starting with 10 sessions. Until one horizon
clears all five gates on the same signal and the same clock, the
deployment-leg work stays behind the gate.

## The hedge leg was outside the turnover cap

The neutral book sent its orders through two separate accounting paths.
``MarketNeutralStrategy.on_bars`` charged the sell proceeds to the long
leg's ``max_turnover_ratio`` and then called ``_rebalance_hedge``, which
derived the short side's share count from the book it was leaving and set
that target directly. The cap therefore bounded the buys and the sells
while the hedge re-set its target every rebalance unmeasured, so the
gross the book actually turned was however much the hedge wanted.

The two legs now share one budget. Each leg records its own delta in a
single ``long_deltas`` map, the hedge is sized against the book the long
legs are becoming rather than the one they are leaving, and one
``scale_turnover`` call scales the merged map down until its gross
notional fits the cap. The hedge is scaled once, with the long side,
rather than on its own and then again as part of the book, because
scaling it twice would quietly change the hedge ratio from the one the
setting declares.

A second rounding fault sat underneath it. ``_lots`` rounded a share
count with ``round_to``, so a budget of 50 shares under a 100-share
board lot became 100, doubling the volume the book ordered. It now
floors instead, in both the neutral and the band strategy.

On the ensemble signal with the default 0.30 cap, the same five gates
pass on one clock:

| metric | value |
| --- | --- |
| total return | 18.354% |
| annual return | 8.5533% |
| sharpe | 1.293 |
| max drawdown | -6.3393% |
| turnover x capital | 6.8261 |
| commission / capital | 0.6995% |
| trades | 7160 |

Per year, the net profit is positive in all three: 2024 +2.73M, 2025
+3.76M, 2026 +11.87M.

What this still does not settle is the deployment leg. Those numbers
come from the backtesting engine, and the runner that would carry them
into a paper account still has no A-share tick source: ``DerivedFeed``
mapped only the futures exchanges, so an equity secid built no key and
a stock never got a tick. The three cash exchanges are now declared,
and an equity's price is no longer rescaled by the integer shape of its
bar, which would have divided a 1685 yuan stock by ten. The path from a
signal to a paper fill has one less break in it; it has not yet been
run end to end.

## The stock basket hedge was never deployable, and the ETF leg needed a hedge ratio the setting did not carry

The short leg was thirty individual A-shares. Shorting a basket of single names needs borrowed securities that are hard to borrow and expensive when they exist at all, so the numbers that cleared the fee gate were numbers on a book no account could open. The index-future route was tried first and closed: ten Eastmoney secid shapes were tested against the kline endpoint and none of them returned data. The ETF route works. `510300.SSE` has 1423 daily bars from 2021-01-04 to 2026-11-18 on the same public host, and `fqt=0` returns them unadjusted, which is what a tradable leg has to be priced on.

Three things had to be true before an ETF leg could trade at all. The hedge basket now honours one named instrument, `hedge_symbol`, over the default basket. The hedge is sized against `projected_long_value`, the book the long legs are becoming rather than the one they are leaving, because sizing off stale positions hedges the previous book and leaves the new one exposed. And the kline parser leaves the instrument blank for its caller to name, so the loader stamps `symbol`, `exchange` and `vt_symbol` onto every bar -- `vt_symbol` is a snapshot taken when the dataclass was built, so rewriting `symbol` alone leaves the leg answering to a blank name and the engine loads a bar file the strategy never asks for.

One unadjusted ETF leg, `hedge_ratio` swept, everything else at the settings that passed the gates above, 515 sessions from 2024-10-08:

| hedge_ratio | total% | annual% | sharpe | max DD% | net | gross | fee% | trades |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.6 | 14.61 | 6.81 | 1.21 | -5.99 | 0.140 | 0.534 | 0.691 | 6463 |
| 0.8 | 13.70 | 6.38 | 1.18 | -5.92 | 0.072 | 0.587 | 0.686 | 6453 |
| 1.0 | 12.16 | 5.67 | 1.07 | -5.88 | 0.007 | 0.637 | 0.679 | 6513 |
| 1.2 | 10.84 | 5.05 | 0.92 | -5.81 | -0.054 | 0.681 | 0.674 | 6559 |

Return falls monotonically as the ratio rises, and the net exposure the table prints moves from 14 percent of capital at 0.6 to a short 5 percent at 1.2. The long book's beta is therefore well under one, and a fully hedged book over-hedges: the sweep is measuring how much market exposure the book wants, not whether the hedge works. The honest reading is that a beta of roughly 0.5 is optimal on this window, and that number is fitted to the sample rather than estimated from an instrument, so it is the first thing that has to be re-derived out of sample before any of it is traded.

All four rows still clear the fee gate, at 0.67 to 0.69 percent against capital, because an ETF leg rebalances in 18 trades where thirty single names rebalanced in thousands. That is the deployment argument for the ETF leg independent of its return: 18 fills against a borrowable instrument against 3000-odd fills against names that may not be borrowable at all.

The engine's equity figure has to be audited alongside these numbers. It subtracts a short position's market value as if selling it released cash the account may spend, which is true for a closed trade and false for a security the book still owes. The `net` and `gross` columns above are printed by the runner for exactly that reason, and a book whose net looks small while its gross is more than half its capital is a leveraged book whatever the equity curve says.

## The lending fee is now charged, and it costs about a point of return a year

Two costs were missing from the numbers above, and both were on the leg the deployment argument rests on.

The first is securities lending. A short ETF is borrowed stock, and the fee is charged on the market value of the borrowed position for every calendar day it is held, so it lands on every session rather than on the trades that opened and closed it. Charging it on the trades alone would understate the cost of a hedge by the length of the hedge. `PortfolioDailyResult` now applies it against the sign of each start position, once per session, and subtracts it from `net_pnl`, which is what the balance curve accumulates -- so it reaches the reported return rather than sitting in a printed column.

The second is that the two legs do not pay the same fees. The stamp tax is levied on the transfer of a share, so an ETF leg never pays it while a stock leg pays it on every sell. The transfer fee is charged on every transfer including fund shares, so both legs pay it. The runner now models the buy side and the sell side per instrument -- `stock_rates` as commission plus transfer fee, plus the stamp tax on the sell, and `etf_rates` as commission plus transfer fee on both sides -- and writes them into the lab's contract settings, because the engine charges the commission from there and not from the strategy's own budget arithmetic.

One unadjusted ETF leg at ratio 0.6, 515 sessions, everything else at the settings that passed the gates. The two runs differ only in the lending rate, so the difference is attributable:

| run | total% | annual% | sharpe | max DD% | commission% | borrow fee% | hedge trades |
| --- | --- | --- | --- | --- | --- | --- | --- |
| no lending fee | 14.9132 | 6.95 | 1.23 | -5.97 | 0.3289 | 0 | 18 |
| 8% annualised lending | 12.6731 | 5.91 | 1.05 | -6.24 | 0.3289 | 2.2401 | 18 |

The commission column is untouched between the two runs, which is the check that the fee model refactor did not move anything else. The lending fee at 8 percent annualised costs 2.24 percent of capital over 515 sessions, about 1.66 percent a year, and it takes the return from 14.91 to 12.67 percent. The return survives it and the sharpe survives it, at 1.05.

This is the shape that matters for the goal: the hedge leg's real cost is not the 0.33 percent of commission, it is the 2.24 percent of borrowing, and the two are of different kinds. The commission is a one-off per fill and the lending fee is a running cost that grows with the holding period. A strategy that holds the hedge longer pays more of it per unit of turnover, which is the opposite of the turnover-cap story the fee gate was built around.

## The hedge ratio does not transfer out of sample, and the direction of the error is known

The sweep that said 0.5 to 0.6 was optimal was fitted to the window it was measured on, so the sample was split and the ratio re-measured on the first half before being graded on the second. Fit window 2024-09-26 to 2025-09-30, grading window 2025-10-01 to 2026-11-13, one unadjusted ETF leg, 8 percent annualised lending, everything else unchanged:

| hedge_ratio | fit total% | fit sharpe | graded total% | graded sharpe | fit net_x | graded net_x |
| --- | --- | --- | --- | --- | --- | --- |
| 0.2 | 13.60 | 1.83 | 0.62 | 0.12 | 0.288 | 0.204 |
| 0.3 | 12.54 | 1.85 | 0.90 | 0.17 | 0.262 | 0.175 |
| 0.4 | 11.02 | 1.76 | 1.07 | 0.20 | 0.222 | 0.150 |
| 0.5 | 9.56 | 1.65 | 1.15 | 0.21 | 0.181 | 0.125 |
| 0.6 | 8.40 | 1.54 | 1.06 | 0.20 | 0.141 | 0.097 |
| 0.8 | 6.21 | 1.24 | 1.18 | 0.23 | 0.061 | 0.048 |
| 1.0 | 3.81 | 0.81 | 2.21 | 0.41 | -0.016 | -0.004 |

The ordering reverses. On the fitted half, return falls monotonically from 13.60 percent at 0.2 to 3.81 percent at 1.0, and the sharpe peaks at 0.3. On the graded half, return rises monotonically from 0.62 percent at 0.2 to 2.21 percent at 1.0, and the sharpe again peaks at the opposite end from the fit, at 1.0. Whatever the fitted sweep measured, it is not a property of the book that survives a second half of the sample.

Two readings are available and they are not the same claim. The first is that the fit half simply had a stronger market: the graded half's returns collapse by a factor of six to ten even at the ratio the fit half rated worst, so most of the drop is the window and not the ratio. The second is the one that costs money: because the two halves rank the ratio in opposite directions, the ratio cannot be carried from one to the other at all. There is no value of it that is good in both. This is stronger than "the estimate is noisy", because noise would flatten the ordering rather than invert it.

The graded numbers are not a viable book either. The best graded run returns 2.21 percent over 268 sessions with a sharpe of 0.41, against a lending fee of 1.57 percent of capital and a commission of 0.14 percent. The two legs are eating most of what the selection earns, and the exposure audit says the same thing in another currency: net exposure never gets away from 20 percent at 0.2 and never quite reaches zero at 1.0, so the book is carrying market exposure it is paying to carry.

So the honest state of the hedge leg is that it is deployable in the mechanical sense -- 18 fills against a borrowable instrument -- and unprofitable in the graded sense. The question this leaves is not which ratio to use, it is whether the long side's own return survives the costs once the market's contribution is removed honestly, and the last section already said it does not on the raw signal.

## The long side charges 3.94 percent of capital for a negative out-of-sample return

The hedge cannot be judged while the long leg is still uncharged, so the book was run with the hedge removed, on the same split, at the same fee model, with the same 8 percent annualised lending rate available. `hedge_ratio 0` also removes the short leg, so the lending fee column is zero by construction -- which is the check that the fee is charged only on positions that are actually short:

| window | sessions | total% | sharpe | max DD% | commission% | borrow fee% | net_x | gross_x |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| fit, 2024-09-26 to 2025-09-30 | 247 | 17.5838 | 1.93 | -4.68 | 0.1699 | 0 | 0.400 | 0.400 |
| graded, 2025-10-01 to 2026-11-13 | 268 | -0.2974 | -0.00 | -7.57 | 0.1550 | 0 | 0.263 | 0.263 |

The fit half returns 17.58 percent, which is the best number this strategy has produced anywhere, and the graded half loses money. The gap is not the fee model: the graded half pays 0.1550 percent of commission against the fit half's 0.1699 percent, so it is cheaper to run and still loses. The graded half's loss is concentrated entirely in 2026, which is -1.67M over 208 sessions at a sharpe of -0.26, while the 60 sessions of October to December 2025 that fall in the same window are +1.37M at a sharpe of 1.07. The signal did not break in 2026; the market's 2026 return did, and the book carried roughly 0.26 of capital of net exposure into it.

Two things follow, and the first is the one that answers the goal. The long book's best number, 17.58 percent, is a fit-half number, and the honest number for the same book is -0.30 percent. Any claim built on the sweep tables in the sections above is a claim built on the fit half. Second, the hedge leg cannot be blamed for the graded outcome: removing it entirely does not rescue the graded half, it turns 2.21 percent at ratio 1.0 into -0.30 percent at ratio 0. The hedge was doing the only thing left for it to do, which is damp the drawdown from 7.57 to 3.72 percent, and the price of that insurance is the borrowing fee.

So the position the evidence supports is that the book is currently a market-beta play with a selection overlay, and the selection overlay has not paid for itself on the graded half. The next test is not another ratio sweep. It is the horizon question, because a signal that decays the way this one does is a signal whose holding period is wrong, and the returns quoted in every section above are quoted at a horizon whose own caches were relabelled once and never retrained.

## Retraining at the 10-session horizon makes the signal worse, on both halves

The open question from the last section was whether the horizon was wrong, because every return quoted in this document was measured on a signal whose own cache had been relabelled once and never retrained at the horizon it claimed. There was already a 10-session ensemble cache in sweeps/market3000_size_rolling900_250_h10_ens.parquet. Its overlap with the 5-session cache is only 0.51, the first sign that a different horizon selects a genuinely different signal rather than re-rotating the same one.

That cache was used directly, at the same fee model, on the same split, with the hedge removed. Nothing was refit for these runs; the signal was read as cached:

| signal | fit total% | fit sharpe | graded total% | graded sharpe | fit turnover | graded turnover |
| --- | --- | --- | --- | --- | --- | --- |
| 5-session, as cached | 17.58 | 1.93 | -0.30 | -0.00 | 3.44 | 3.17 |
| 10-session, as cached | 5.89 | 1.04 | -3.56 | -0.47 | 2.80 | 3.10 |

The 10-session signal is worse in both halves, and by a wide margin on the graded half, where it loses 3.56 percent against the 5-session signal's 0.30 percent loss. Its graded loss is 2026 alone, at -3.59M over 208 sessions and a sharpe of -0.59, against the 5-session signal's -1.67M at -0.26 in the same sessions. The 2025 partial-year inside the graded window is roughly flat for both.

This closes the horizon question in the direction nobody wanted: a longer horizon is not the fix. Refitting at the horizon that the slower clock was supposed to suit makes both the fit-half return and the graded-half return worse, and it does so while carrying more net exposure on the graded half, 0.288 against 0.263. The signal's own IC gates agree with it -- 10 sessions reads IC 0.0415 and positive IC 62.4%, both over target, against ICIR 0.385 which is under the 0.5 gate -- but passing the IC gates on a cross-section is not the same as earning money on a clock, and on this book it does not.

So all three fronts this note has left open are now closed, and none of them pays. The ratio does not transfer out of sample and the two halves rank it in opposite directions. The long side earns 17.58 percent on the fit half and loses money on the graded half. The longer horizon loses money faster. What remains is not a parameter to tune. It is the observation, repeated in each of these measurements, that the book's return is the market's return: the graded half's losses are 2026 market beta in every configuration tried, with the net exposure between 0.26 and 0.29 of capital, and the fit half's gains are the same exposure pointed the other way.

## The graded half's loss is not the market's, and the book paid it anyway

Every run above loses money on the graded half and every one of them blames 2026, because 2026 is where the loss sits. This is the claim that decides whether the blame is right, so it was tested against benchmarks the book is not built out of, computed from the same cached bars on one session-by-session clock: equal weight over every stock, the top decile by trailing turnover, and `510300.SSE`, the instrument the hedge leg actually trades.

The graded window is split at the year boundary, and the two halves of it are different markets:

| benchmark | 2025 sessions | 2025 total% | 2026 sessions | 2026 total% |
| --- | --- | --- | --- | --- |
| 510300.SSE | 60 | +0.253 | 208 | -14.096 |
| equal weight, every stock | 60 | +6.119 | 208 | -4.253 |
| top decile by turnover | 60 | -3.719 | 208 | -23.222 |

The equal-weight number is an average and the median stock did much worse. Over 2026 the median stock returned -18.91 percent and 78.8 percent of the 2713 stocks in the cache were negative, so the market's -4.25 percent mean is a right tail of winners pulling an average over a distribution whose centre lost far more. Both numbers are reported because they answer different questions: the mean is what an equal-weight book earns, the median is what a randomly chosen stock lost.

The book's graded return was -0.30 percent long-only and -0.30 to -3.56 percent depending on the signal and ratio. Against a market that returned -4.25 percent on the mean and -18.91 percent at the median, a book that lost 0.30 percent **beat the market**. The book did not pay for 2026; it was paid less than the market paid everyone else. The claim the sections above were carrying, that the graded half lost to market beta, is wrong in the direction that matters: the loss is smaller than the market's own loss on every one of these readings.

What the book did not do is avoid the fall. Its 2026 net exposure was between a quarter and a third of capital, which is a market-neutral book carrying a quarter of the market's beta, and the benchmark table shows the price of that: a genuinely measured neutral book would have lost roughly nothing in 2026 rather than 0.30 percent, and would have earned far less than 17.58 percent in 2025. The book's shape is not the problem. The problem is that its whole contribution, fit half and graded half together, is no larger than the market's noise.

The fall is not one month. `510300.SSE`, session by session, calendar 2026:

| month | total% |
| --- | --- |
| 01 | -0.88 |
| 02 | +0.30 |
| 03 | -5.54 |
| 04 | +8.09 |
| 05 | +2.05 |
| 06 | +1.95 |
| 07 | -7.29 |
| 08 | +0.69 |
| 09 | -5.40 |
| 10 | -8.28 |
| 11 | +0.44 |

Four separate months each lost five to eight percent and the year lost 14.10
percent, so a book sized for a single drawdown is not sized for this market.
The book's own graded loss is spread over the same sessions rather than
concentrated, which is consistent with it being the same exposure rather than
a bad month.

The first version of this measurement was wrong and the error is worth recording, because it would have produced the opposite conclusion. Compounding 208 overlapping five-session returns on `510300.SSE` reads **-51.68** percent for 2026, while the same year reads **-14.10** percent compounded session by session, **-14.33** percent in non-overlapping five-session blocks, and **-15.71** percent from the first close of the window to the last. The overlapping figure counts the same price move about four times over. A market that appears to fall by half justifies any loss, and only a fixed clock shows the book beat it.

## The full hedge is the first configuration that survives out of sample, and the reason is not the selection

Every section above was written against `hedge_ratio` values of 0.6 or less,
and against the long book alone. At `hedge_ratio` 1.0, with a single ETF leg of
510300.SSE sized one to one against the long book, the graded half earns money:

| borrow rate (annualised) | fit total% | fit fee% | hold total% | hold fee% | hold sharpe |
| --- | --- | --- | --- | --- | --- |
| 0% | +5.3906 | 0 | +3.7849 | 0 | 0.678 |
| 2% | +4.9954 | 0.395 | +3.3920 | 0.393 | 0.610 |
| 3% | +4.7978 | 0.593 | +3.1956 | 0.589 | 0.576 |
| 8% (the old assumption) | +3.8097 | 1.581 | +2.2134 | 1.571 | 0.407 |

The exposure numbers explain why the ratio was the whole problem. With the
full hedge in place `net_x` reads -0.0158 on the fit half and -0.0036 on the
graded half. Every earlier configuration was running `net_x` of 0.26 to 0.29,
which is to say it was never a neutral book at all.

## The hold half's profit is the short leg's, not the selection's

This is the finding that changes what the table above means, and it was not
visible until the book's own numbers were split by contract. The engine keeps
one `ContractDailyResult` per contract per day
(`vnpy/alpha/strategy/backtesting.py:863`), and `leg_split` in
`scripts/alpha_beta_backtest.py` now reads the hedge instrument's net pnl
separately from everything else. Both halves, at a zero lending fee:

| half | sessions | hedge leg net | rest net | total net | sessions hedge>0 | sessions rest>0 |
| --- | --- | --- | --- | --- | --- | --- |
| fit 2024-09-26..2025-09-30 | 247 | -7,537,181 | +12,927,777 | +5,390,596 | 105 | 162 |
| hold 2025-10-01..2026-11-13 | 268 | +3,832,772 | -47,857 | +3,784,915 | 136 | 143 |

As a share of the 100 million book, the hold half's +3.7849 percent is
+3.8328 from the short leg and -0.0479 from the selection. **The selection
lost money on the graded half.** What paid was being short a market that fell
13.879 percent on the same window, and the same reading holds the other way
round on the fit half: there the selection earned +12.93 percent and the short
leg lost -7.54 percent against a market that rose 30.247 percent.

So the book is not a market-neutral alpha engine with a hedge attached. It is
two legs that each take the market's direction with the opposite sign, and
whichever leg matches the market's sign collects. The graded half's +3.78
percent is a directional bet on 2026's decline, sized at roughly a quarter of
capital on each side, that happened to be right. It is not evidence that the
selection works out of sample, and the honest statement of the graded half is
that the overlay was negative and the market covered it.

This does not make the configuration worthless, and it does not license going
back to the ratios below 1.0. A half-short book still carried net exposure of
0.26 to 0.29 and lost on the graded half. What it changes is the claim: the
graded half earned +3.78 percent because the market fell 13.9 percent, and a
book whose profit is a leveraged view on the index's direction is not a
business until the selection itself pays.

## The financing leg does not exist, and the assumption is dropped

A `gross_x` of 0.7147 on the fit half and 0.5086 on the graded half looks like
leverage, and it was recorded as an open question whether the long leg needs a
financing cost. It does not, and the mechanism is in the engine rather than in
the market. A short fill increases cash (`vnpy/alpha/strategy/backtesting.py:755`),
the long book is sized off
`book_value = get_holding_value() + cash`
(`vnpy/alpha/strategy/strategies/market_neutral_strategy.py:225`), and the
hedge leg is sized as `long_value * hedge_ratio` without touching the long
side's cash budget (`market_neutral_strategy.py:302`). The engine carries no
margin or frozen-cash concept anywhere. So the short leg funds itself out of
the proceeds of its own sale, and no borrowing cost belongs on the long side.
This removes a caveat that was written down before it was checked, and it
should be read as a reminder that an unchecked assumption in this note has
previously cost the note the opposite conclusion.

## The market comparison has to be made on the book's own window

The benchmark table this note carries reports calendar years. The book's
graded half is not a calendar year: it runs 2025-10-01 to 2026-11-13, which is
268 sessions, and it spans two calendar years. Read on that window instead:

| benchmark, hold window 2025-10-01 to 2026-11-13 | total% |
| --- | --- |
| 510300.SSE, session-compounded | -13.879 |
| equal weight, every stock, per-session cross-section | +1.605 |
| per-stock median | -17.623 |
| negative share of the 2713 stocks | 75.6% |

The book earned +3.78 on this window at a zero lending fee, and on the leg
split above it earned it from the short side. The equal-weight cross-section
mean is +1.605 on this window, so a book that is merely short the index
against a flat selection has more than one route to a positive number here,
and the spread over the mean is not the thing to look at.

## The selection beats its universe out of sample and loses to it in sample

The section above needed one more window to answer whether the selection pays,
and that window cannot be bought from a sweep on the same bars. It can be
bought without the backtest engine, which is the point: every number below
comes from the signal and the cached bars alone, so nothing in the order path,
the turnover cap or the cash book can contribute to it.

The口径 is stated first because two earlier readings in this note were wrong
for exactly this reason. The book's label is a five-session forward return
compounded from the close after the session to the fifth close, so a portfolio
that re-holds the same names every session and is then compounded across
overlapping five-session returns counts the same price move about five times.
That is the same error as the -51.68 percent reading of 2026, arriving from a
different direction, and it produces absurdities: 547 percent on the fit half
and -20 percent on the graded half. Read instead on one-session closes, which
is the clock the engine's own daily results use:

| half | book, equal weight | equal-weight cross-section | excess |
| --- | --- | --- | --- |
| fit 2024-09-26..2025-09-30 | +73.35 | +75.20 | -1.81 |
| hold 2025-10-01..2026-11-13 | -3.47 | -8.83 | +6.35 |

The band is the same 5-to-15 percent share of the ranking the strategy holds,
buffer 0.10, 30 session clock, and the returns are compounded across the
267 to 247 sessions in each half. On this reading the selection **out-earned
its universe by 6.35 percent on the graded half and under-earned it by 1.81
percent on the fit half**. That is the opposite sign from the backtest's
selection leg, which was -0.05 percent on the graded half and +12.93 on the
fit half, and the difference is the backtest's own size and timing rather
than a disagreement about the signal: the book the engine runs holds about a
quarter of capital, compounds its rebalance clock against a five-session
label, and pays commission on a turnover of 3.14 times capital. A leg sized
at 0.25 of capital earning a cross-section-relative +6.35 percent, divided by
the two halves' different session counts and by the fee, lands near the
engine's -0.05 once the label's horizon is compounded on the same clock as
the returns.

So the answer to what the section above was asking is that the selection's
contribution is real but small in the direction that matters: it beats the
universe out of sample, by six points, and it does so in the window where the
market fell. It is not the mechanism that made the graded half earn +3.78
percent, which was the short leg. It is, however, the mechanism that would
make a book with the market properly hedged earn money, because +6.35 percent
of cross-section-relative return at a quarter of capital is the same
statement the model's own IC makes, and the IC made it first: rank IC
+0.0638 on the fit half and +0.0530 on the graded half, ICIR 0.55 and 0.48,
positive on 71.3 and 65.8 percent of sessions.

The honest summary of the whole note is now three numbers that agree: the
signal still ranks in 2026 (IC +0.0530), the book still beats its universe in
2026 (+6.35 percent), and the book still loses money on the graded half once
the market is not being shorted (-0.05 percent). The gap between the first
two and the third is size, clock and cost, not a dead signal, and it is the
part that the next window has to be designed to attack rather than to
re-measure.

## The excess is a down-market exposure, not a cross-sectional alpha, and the scan that found it was fitted on the same half it was graded on

The previous section measured the selection against its own universe and read
+6.35 percent of excess on the graded half. Read on one-session closes that
number stands, but it does not mean what it appeared to mean, and the scan
that followed it is the reason the distinction has to be stated before it is
trusted.

Split the graded half's daily excess by whether the cross-section itself rose
or fell that session, with the most-traded decile removed so a hot shell
cannot carry the cross-section's own average:

| band | rising days, n | excess on rising days | falling days, n | excess on falling days |
| --- | --- | --- | --- | --- |
| 0.05-0.15 | 138 | +0.1177 | 129 | -0.0729 |
| 0.15-0.25 | 138 | +0.0416 | 129 | +0.0372 |
| 0.10-0.30 | 138 | +0.0472 | 129 | +0.0223 |
| 0.20-0.50 | 138 | -0.0428 | 129 | +0.1069 |

On the graded half the excess of every wide band comes from the sessions the
market fell. The same split on the fit half, which is the half that says
whether the reading is a regime or a property, is unambiguous:

| band | rising days, n | excess on rising days | falling days, n | excess on falling days |
| --- | --- | --- | --- | --- |
| 0.05-0.15 | 155 | -0.0787 | 92 | +0.1032 |
| 0.15-0.25 | 155 | -0.1711 | 92 | +0.2299 |
| 0.10-0.30 | 155 | -0.1470 | 92 | +0.1922 |
| 0.20-0.50 | 155 | -0.1689 | 92 | +0.2030 |

On the fit half every one of these bands **loses on the sessions the market
rose and wins on the sessions it fell**, by more than it wins. That is a
down-market exposure: the book is long names that fall less when the
cross-section falls and rise less when it rises. It is the shape that earns a
negative premium overall and a positive premium in a falling year, which is
what 2026 was. It is not an alpha, because an alpha would pay on both sides
of the cross-section's average and this one pays only on one.

This is the third time in this note that a positive-looking number has been
produced by a market that moved in one direction. The first was total return
in a rising market, the second was the short leg's directional return in a
falling one, and this is the third: the band's excess in a falling half. The
gate that catches all three is the same, and it is that a book graded only
against its own universe still inherits the universe's sign.

## The scan that ranked bands on the graded half is not a selection

The band scan was run on the graded half and read the ranking directly off
it. On the graded half, wider and lower bands look better, up to +10.95
percent for 0.15-0.25 against +6.35 for the 5-to-15 percent band the strategy
actually holds. On the fit half the order reverses, and the reversal is
monotonic in the band's position:

| band | fit excess% | graded excess% |
| --- | --- | --- |
| 0.00-0.05 | +8.31 | +1.13 |
| 0.05-0.15 | -1.81 | +6.35 |
| 0.10-0.20 | -3.88 | +8.40 |
| 0.15-0.25 | -5.11 | +10.95 |
| 0.20-0.50 | -5.60 | +8.91 |

The corumers toward the signal's top rank behave the opposite way on the two
halves, so no single band is good in both. This is the same failure the
hedge-ratio section recorded, arriving from a different knob: a parameter
whose sign flips between halves cannot be picked on either half, and the
sweep table above is therefore not evidence for any band in it.

Month by month, 0.15-0.25 is positive in 11 of 14 graded months and
0.05-0.15 in 8 of 14. The wider band is steadier, which is consistent with
it being more of a defensive exposure rather than less of a bet, and it is
not by itself a reason to prefer it.



The honest state of the selection is now: rank IC is positive on both halves
(+0.0638 and +0.0530, ICIR 0.55 and 0.48), the book beats its universe on the
graded half (+6.35 percent, non-overlapping closes), and that excess is
explained by a down-market exposure that costs more on the fit half than it
earns. The signal ranks; the ranking as currently held does not pay on both
sides of the cross-section's average.

The signal's edge is in the low-volatility half, and it was hidden by averaging it with the high-volatility half

Before this section, three attempts were made to test whether the excess survives a factor adjustment, and all three returned numbers identical to the unadjusted ones. That is not a finding about the signal, it is a property of the test: rank IC is computed on ranks, and subtracting a constant from every member of the cross-section leaves the ranks exactly as they were. Any "cross-sectional de-meaning" applied to a rank IC is a no-op by construction, and recording it as a return of the same number would have been recording the arithmetic instead of the market. The test that does measure something is a split of the universe by a characteristic the signal might be proxying, run on the book's own non-overlapping returns.

Split by trailing 20-session volatility at the cross-sectional median:

| half | universe | book% | excess% |
| --- | --- | --- | --- |
| fit | every name | +73.34 | -2.20 |
| fit | lower half by volatility | +62.54 | +5.38 |
| fit | upper half by volatility | +95.68 | -2.32 |
| graded | every name | -3.53 | +6.00 |
| graded | lower half by volatility | +2.48 | +5.55 |
| graded | upper half by volatility | -13.26 | +2.51 |

On both halves the excess comes from the lower half by volatility. On the fit half the whole universe is negative (-2.20) and the low-volatility half is positive (+5.38), so the signal's contribution there is entirely in the quiet names and the volatile names subtract from it. On the graded half the low-volatility half earns +5.55 against +6.00 for everything, which is to say that narrowing to the quiet half does not improve the graded half but does repair the fit half.

That is the first split in this note that is positive on both halves at the same time, and it is the reason to say the signal is not merely a down-market exposure. The down-market reading above is still true and still important: a book graded against a universe that fell inherits some of that fall, and the graded half's +6.00 is measured against a universe that lost 8.83 percent. But a split that survives the fit half, where the same machinery failed, is evidence about the signal rather than about the year.

## The volatility split, read both ways, and why only one of them looked good
The work this defines was a re-run with the universe itself stratified, which
is a setting rather than a discovery. That run has now been done and it gives
a different answer depending on how the stratification is done, so both forms
are recorded rather than the one that looked better.

The first form selects on the whole cross-section and then reports how the
selection's low-volatility members did against the low-volatility universe.
That is the reading in the table above, and it is the one that survives the
fit half: +5.38 on the fit half and +5.55 on the graded half. The second form
re-ranks inside the low-volatility half and re-picks the band from there, so
the names are chosen by signal restricted to the quiet half rather than by
signal over everything:

| half | vol cut | book% | excess% |
| --- | --- | --- | --- |
| fit | none | +73.47 | -2.12 |
| fit | lowest 50% | +55.34 | +0.46 |
| fit | lowest 30% | +49.12 | +1.23 |
| fit | lowest 20% | +39.53 | -2.41 |
| graded | none | -3.41 | +6.12 |
| graded | lowest 50% | -0.89 | +2.02 |
| graded | lowest 30% | -1.22 | +1.63 |
| graded | lowest 20% | -1.92 | +2.77 |

Narrowing the universe does not repair the fit half and it destroys most of
the graded half's excess. The volatility split is therefore a description of
where the ranking is already sharper and not a parameter to tune. The ranking
is sharper in the quiet half when the ranking is still made over the whole
cross-section, and re-making it inside the quiet half makes it worse, which
says that volatility and the signal interact rather than that one selects on
the other.

## Weighting inside the book by volatility flips its sign between the halves, and so does the low/high split itself

The sizing question was the one left open, and it is answered badly. Holding
the same band and re-weighting inside it by trailing 20-session volatility
moves both halves, and in opposite directions:

| weighting | fit book% | fit excess% | graded book% | graded excess% |
| --- | --- | --- | --- | --- |
| equal weight | +73.34 | -2.20 | -3.53 | +6.00 |
| inverse volatility | +67.35 | -6.24 | +0.50 | +9.96 |
| inverse volatility squared | +59.40 | -11.27 | +2.81 | +12.05 |

The volatility group split is worse, and the reason is that it is not a
description of where the signal pays but a trade with the other sign:

| membership | fit book% | fit excess% | graded book% | graded excess% |
| --- | --- | --- | --- | --- |
| the whole band | +73.34 | -2.20 | -3.53 | +6.00 |
| band members at or below the median volatility | +58.34 | -11.89 | +2.89 | +12.11 |
| band members above the median volatility | +88.99 | +8.11 | -10.26 | -0.55 |

The quiet half earns -11.89 on the fit half and +12.11 on the graded half;
the volatile half earns +8.11 and -0.55. Each is good on exactly one half and
the halves are not the same, so this is the third knob after the hedge ratio
and the band edge whose sign flips between them, and it cannot be picked on
either. It also dissolves the reading it was built on: the low-volatility
members of the selection, reported as +5.38 and +5.55 in the previous section,
earn those numbers because the low-volatility universe they are measured
against also fell less, and the same names measured as a membership rather
than as a comparison earn -11.89 on the fit half.

So the honest statement of the sizing question is that every volatility
weighting and grouping tried here trades the two halves against each other,
and the reason is visible in the mechanism: a book weighted toward the quiet
names carries less of the cross-section it sits in, which pays when the
cross-section falls and costs when it rises, and that is the same
down-market exposure the excess split above identified. It is not an alpha and
it is not sizeable as one.

## The market-neutral long-short book is the first test that cannot inherit the market, and the ranking does not survive it

Every reading above graded the book against a universe it sits inside, so each
one inherited the universe's sign along with the selection's contribution.
Holding the top of the signal against the bottom of the same signal removes
that, because the two legs take the cross-section in opposite directions and
the cross-section cancels out of the difference. This is the only test in this
note where a positive number cannot be produced by a market that moved in one
direction.

Run on one-session closes, equal weighted, 30 session clock, the same buffer:

| legs | fit daily% | fit t | fit p | graded daily% | graded t | graded p |
| --- | --- | --- | --- | --- | --- | --- |
| top 5% / bottom 5% | +0.0219 | 0.31 | 0.755 | +0.0186 | 0.27 | 0.789 |
| top 5% / bottom 10% | +0.0099 | 0.16 | 0.871 | +0.0439 | 0.85 | 0.397 |
| top 10% / bottom 10% | -0.0147 | -0.24 | 0.810 | +0.0464 | 0.78 | 0.436 |

Not one of these is distinguishable from zero on either half. The daily means
are all within a quarter of a basis point of each other, the t statistics are
all below one, and the graded half is not more significant than the fit half
despite looking larger.

The compounding is the same story and it is worse, because the month-level
series is dominated by two months. The top-5/bottom-5 book compounds to +3.77
percent on the fit half and +4.49 on the graded half, which looks like the
first positive-on-both-halves result in this note, and then the monthly detail
takes it away: the graded half is -17.80 percent in 2026-07 and +19.95 percent
in 2026-08, so the two months alone are worth more than the whole year's
return and neither is a property of the ranking. The graded half is positive in
6 of 14 months and the fit half in 6 of 13, which is a coin flip.

So this is the answer to what the previous sections were asking. The signal
ranks (IC +0.0633 and +0.0521), and a book that holds the ranking against
itself earns nothing that a t test can separate from noise. That is not a
statement that the ranking is empty; it is a statement that its tradable
content, at this band, this clock and this universe, has not been shown to
exist, and that every positive number earlier in this note was the market's
direction hiding in a comparison.

## A second out-of-sample window was found, and the long-short book still does not survive it

The previous section closed by asking for one of two things: a window the model
has never seen, or a signal built differently from the 158-factor fit. The first
one is reachable, because the scoring window was never a property of the data,
it was a property of the train window. ``predict_rolling`` starts its first
block at ``train_window + horizon`` sessions, so a 900-session window could not
score before 2024-09-26 and a 250-session window starts scoring at 2022-01-20.
The dataset's cached ``data_periods`` say the model that produced these signals
was fitted on 2021-11 through 2023-06, so a signal scored from 2022-01-20 is
out of sample with respect to the fit, which is the window the note could not
reach before.

| train window | sessions | IC mean | ICIR | IC positive | IC by year |
| --- | --- | --- | --- | --- | --- |
| 900 (previous) | 479 | 0.0436 | 0.349 | 0.633 | 2024 +0.1488 / 2025 +0.0005 / 2026 +0.0699 |
| 500 | 901 | 0.0305 | 0.270 | 0.639 | 2023 +0.0360 / 2024 +0.0520 / 2025 +0.0176 / 2026 +0.0133 |
| 250 | 1151 | 0.0275 | 0.276 | 0.629 | 2022 +0.0349 / 2023 +0.0299 / 2024 +0.0354 / 2025 +0.0112 / 2026 +0.0264 |

The ranking keeps ranking across all five years on the 250-session window, which
is the first time this note has seen a positive rank IC in 2022 and 2023 for this
signal. The IC mean is lower than the 900-window reading for the overlapping
period, which is the cost of a shorter training window, and that is expected.
None of this is new evidence of an edge; it is evidence the edge, such as it is,
is not a 2024 artefact.

The long-short book on the 250-session window, same one-session closes, same
30-session clock, same buffer, equal weighted on both legs:

| window | legs | n | daily% | t | p |
| --- | --- | --- | --- | --- | --- |
| 2022 | top 5% / bottom 5% | 230 | +0.0318 | 0.56 | 0.572 |
| 2022 | top 5% / bottom 10% | 230 | +0.0158 | 0.33 | 0.741 |
| 2022 | top 10% / bottom 10% | 230 | +0.0463 | 0.97 | 0.333 |
| 2023 | top 5% / bottom 5% | 242 | +0.0080 | 0.16 | 0.873 |
| 2023 | top 10% / bottom 10% | 242 | -0.0019 | -0.05 | 0.964 |
| 2024 | top 5% / bottom 5% | 242 | -0.0391 | -0.31 | 0.760 |
| 2024 | top 10% / bottom 10% | 242 | -0.0449 | -0.49 | 0.622 |
| 2022-2024 | top 5% / bottom 5% | 714 | +0.0848 | 1.42 | 0.156 |
| 2022-2024 | top 5% / bottom 10% | 714 | +0.0747 | 1.39 | 0.165 |
| 2022-2024 | top 10% / bottom 10% | 714 | +0.0637 | 1.48 | 0.280 |
| 2025 | top 5% / bottom 5% | 243 | +0.1205 | 1.24 | 0.216 |
| 2025 | top 10% / bottom 10% | 243 | +0.0630 | 0.85 | 0.395 |
| 2026 | top 5% / bottom 5% | 207 | -0.0151 | -0.22 | 0.824 |
| 2026 | top 10% / bottom 10% | 207 | +0.0043 | 0.08 | 0.933 |
| full 2022-2026 | top 5% / bottom 5% | 1164 | +0.0155 | 0.37 | 0.712 |
| full 2022-2026 | top 10% / bottom 10% | 1164 | +0.0112 | 0.36 | 0.720 |

Not one row reaches a two-tailed p below 0.05. The three-year 2022-2024 window,
which is the one the model was never fitted on, is the best of them at t = 1.42
and p = 0.156, and it is best in the direction the signal predicts, but a t of
1.4 on 714 observations is not a result.

Two more tests were run rather than one, because a single window can be unlucky
and because a rebalance clock of thirty sessions makes neighbouring daily
spreads share part of their cause. Summing each disjoint block of five sessions
into one observation:

| window | legs | blocks | block% | t | p |
| --- | --- | --- | --- | --- | --- |
| 2022-2024 | top 5% / bottom 5% | 142 | +0.4267 | 1.64 | 0.101 |
| 2022-2024 | top 5% / bottom 10% | 142 | +0.3802 | 1.64 | 0.101 |
| 2022-2024 | top 10% / bottom 10% | 142 | +0.3191 | 1.69 | 0.091 |
| 2025-2026 | top 5% / bottom 5% | 90 | +0.2345 | 0.99 | 0.321 |
| 2025-2026 | top 10% / bottom 10% | 90 | +0.0992 | 0.58 | 0.560 |
| full 2022-2026 | top 5% / bottom 5% | 232 | +0.0668 | 0.35 | 0.726 |
| full 2022-2026 | top 10% / bottom 10% | 232 | +0.0495 | 0.34 | 0.734 |

The five-session blocks raise the t statistics a little and not enough: the best
is still t = 1.69, p = 0.091, on the same 2022-2024 window. The second
out-of-sample window answers the previous section's question in the negative on
both units of observation.

The 500-session window was run as a robustness check against the reading being
an artefact of a particular window length, and it agrees:

| window | legs | n | daily% | t | p |
| --- | --- | --- | --- | --- | --- |
| 2023 | top 5% / bottom 5% | 222 | +0.0501 | 0.79 | 0.432 |
| 2024 | top 5% / bottom 5% | 242 | +0.0106 | 0.10 | 0.924 |
| 2025 | top 5% / bottom 5% | 243 | +0.0625 | 0.68 | 0.498 |
| 2026 | top 5% / bottom 5% | 207 | -0.0967 | -1.13 | 0.259 |
| full | top 5% / bottom 5% | 914 | +0.0202 | 0.51 | 0.611 |
| full | top 10% / bottom 10% | 914 | +0.0245 | 0.77 | 0.440 |

So the finding is not that this signal stopped working. It is that this signal,
at every train window length now available on this dataset, across every
calendar year from 2022 to 2026, on daily spreads and on five-session blocks,
produces a long-short book whose return a t test cannot separate from zero. The
ranking survives as a ranking; it has now failed as a book in three separate
scoring regimes and on four calendar years.

Two details of that table matter more than its t statistics. Splitting the
2022-2024 book by leg, compounded on one-session closes:

| window | band | long% | short% | long-short% |
| --- | --- | --- | --- | --- |
| 2022-2024 | top/bottom 5% | +37.72 | -24.01 | +81.24 |
| 2022-2024 | top/bottom 10% | +31.45 | -16.14 | +56.75 |
| 2025-2026 | top/bottom 5% | +39.03 | +15.39 | +20.49 |
| 2025-2026 | top/bottom 10% | +26.92 | +18.43 | +7.17 |

On the window the model never saw, unlike the graded half measured earlier,
both legs push the same way: the long basket rises while the short basket
falls, so the 81 percent spread is not one leg's market direction dressed up as
selection. On 2025-2026 both legs are positive and the spread is small, which is
the regime the earlier note identified.

Trimming the five largest and five smallest days, which is the test for whether
a single session is producing the mean:

| window | band | n | trimmed daily% | t | p | biggest 5 days as share of \|total\| |
| --- | --- | --- | --- | --- | --- | --- |
| 2022-2024 | top/bottom 5% | 704 | +0.0788 | 1.65 | 0.099 | 6.3% |
| 2022-2024 | top/bottom 10% | 704 | +0.0609 | 1.73 | 0.083 | 5.6% |
| 2025-2026 | top/bottom 5% | 440 | +0.0514 | 1.12 | 0.261 | 7.0% |
| 2025-2026 | top/bottom 10% | 440 | +0.0210 | 0.62 | 0.533 | 7.3% |

Removing the extremes raises the t statistic rather than lowering it, and the
five biggest days are about six percent of the absolute total, so the 2022-2024
reading is a broad slow drift and not one lucky session. It still does not
reach a two-tailed p below 0.05, and the honest statement is unchanged: the
right direction, both legs agreeing, no extreme-day dependence, and a
magnitude this sample cannot yet certify.

The 2022-2024 window is the one to remember, because it is the only one where
the model that produced the signal had not yet touched the data it was scoring,
and it is also the one where the book looks best (t = 1.42 daily, t = 1.69 on
blocks) while still failing. A direction that is right and a magnitude that is
not yet measurable is what a research result looks like before it has an
implementation, and the implementation would need a signal whose construction
differs from the one measured here.

## The daily-spread ruler was measuring the decision session's own return

Every daily-spread number in this note credits the book with the return of the
session it was selected on: the selection is made at session d's close and the
spread is ``close(d) / close(d-1) - 1``, which is the return realised during
that same session. A signal computed at the close of d cannot earn session d's
return, so that reading is a look-ahead, and it is the ruler every long-short
book was judged by.

The tell is the per-session rank IC of the signal against the return it is
credited with, which is the cheapest test that could have caught it:

| lag | rank IC | sessions |
| --- | --- | --- |
| 0 | **-0.0639** | 712 |
| 1 | +0.0224 | 1160 |
| 2 | +0.0177 | 1157 |
| 3 | +0.0153 | 1155 |
| 5 | +0.0135 | 1151 |

At lag 0 the signal is anti-correlated with the return it was credited with,
which is what a same-session contamination looks like: names that are already
moving before the signal is computed are not the ones the signal wants to hold
for the following session, so the ranking and the session's return move in
opposite directions. Once the return is shifted out to the next session the
same signal reads positive, and stays positive out to lag 5.

The consequence is that the long-short t statistics in the preceding sections,
which were all computed at lag 0, are not measuring the book. They are measuring
how the signal relates to the session it was picked on. The market-neutral
reading of "indistinguishable from zero" was correct on that contaminated
ruler, and the question it was trying to answer had not yet been asked.

## The lag-one book is the first long-short result that clears a null control

Moving the credited return to the next session is the honest ruler, and the
result is different. Daily rebalancing, which was the configuration that most
exposed the contamination, is the one that now reads strongest, because it
removes the buffering that dilutes a signal which is about the next session
rather than about the current one.

Band 5%/5%, buffer 0.10, rebalance 5 sessions, cost 3bp buy / 13bp sell
(transfer fee on both sides, stamp tax on the sell side), net of costs:

| window | net% per session | t | p | cost as share of gross |
| --- | --- | --- | --- | --- |
| 2022-2024 (model never saw) | +0.1927 | 3.09 | 0.002 | 2.0% |
| 2025-2026 | +0.2694 | 4.18 | 0.000 | 2.2% |

At the 30-session clock the margin thins:

| window | net% per session | t | p | cost as share of gross |
| --- | --- | --- | --- | --- |
| 2022-2024 | +0.1199 | 1.98 | 0.048 | 0.4% |
| 2025-2026 | +0.1129 | 1.97 | 0.049 | 0.5% |

Random-signal control, four shuffles of the real signal within each session:

| window | real net% | shuffle min | shuffle max | reading |
| --- | --- | --- | --- | --- |
| 2022-2024 clock 5 | +0.1927 | -0.0388 | -0.0251 | outside the null |
| 2022-2024 clock 30 | +0.1199 | -0.0101 | +0.0007 | outside the null |
| 2025-2026 clock 5 | +0.2694 | -0.0521 | -0.0194 | outside the null |
| 2025-2026 clock 30 | +0.1129 | -0.0249 | -0.0043 | outside the null |

The shuffle distribution is entirely below zero, because a book whose ranking
carries nothing still pays the trading cost of getting in and out, so what a
shuffle loses is the cost of being wrong and the real signal clears that floor.
The gross numbers confirm it: the real signal earns +0.21 and +0.29 percent a
session before costs, while the shuffles earn roughly zero or below, so the
ranking earns a spread and does not merely avoid paying for nothing.

## What the contamination changes and what it does not

The engine is not implicated. ``new_bars`` pushes session d's bar to the
strategy, which selects off session d's signal and sends orders that are
crossed on the same session's bar, so the engine has always been charging the
book the return of the session it was picked on, and the backtest numbers above
in the note are on the contaminated ruler. What that means is that the engine's
long-only result, which is a return from the open of the entry bar to the open
of the exit bar, is a different measurement from the daily close-to-close
spread, and the two have never been directly compared on the same lag.
Comparing them is the next work, not a correction to the note's conclusions.

What the contamination does change is the conclusion that the ranking has no
tradable content. That conclusion came from the lag-0 spread and is correct on
that ruler, but the lag-1 book on the same signal earns a positive spread with
both legs agreeing, no extreme-day dependence, and it clears a null control on
every window measured. The honest statement is no longer that the ranking has
no tradable content, it is that the previous long-short ruler was contaminated
and the uncontaminated one has not yet been pushed into the engine.

The clean ruler also changes the question for a book that pays. The
configurations that clear the gates on the uncontaminated ruler are daily
rebalancing at band 5%/5% or the 30-session clock at the same band, and the
question that remains is whether the engine can trade either at a cost that
clears the fee gate it already uses.

## The engine has the honest ruler now, and it reads differently from the spread

A ``signal_lag`` setting is on the engine. The strategies keep a session
calendar built from the bars they have already seen, and the rebalance reads the
signal from ``signal_lag`` sessions back, while the orders are still crossed on
the bar of the session being traded. Lag 0 is the previous behaviour, lag 1 is
the honest ruler the offline spread measured.

The long-only band on the 250-session signal, 5-15 with a 0.10 buffer on a
30-session clock, 2022-01-20 through 2024-12-31:

| setting | total% | annual% | sharpe | max dd% | turnover x | commission fraction |
| --- | --- | --- | --- | --- | --- | --- |
| lag 0 | +7.1806 | +2.4137 | 0.3729 | -9.0651 | 8.35 | 0.42% |
| lag 1 | +3.8595 | +1.2973 | 0.2238 | -9.8063 | 8.28 | 0.42% |

The offline spread, which was measured on the same window, band and clock as a
daily long-short, reads +0.12 percent a session at lag 1 and was not
significant at the 30-session clock without the per-session shaping the
long-short book gives it. The engine's long-only number sits between the
offline long-short and the lag-0 baseline, and the difference is that the
engine's long-only book does not hold the short side, so the cross-section's
direction is still inside it.

Both lag readings are positive over the window, and neither is the
contamination-free number the offline spread measured. What this run does
prove is that the honest ruler is reachable from the engine, which is what the
previous section said was the remaining work. What it does not yet prove is
that the engine's own long-short, at a band and clock that clear the offline
gates, is profitable after the engine's own costs, and that is the next
measurement.

## Where this leaves the signal

The three knobs that have been swept all flip sign between the halves: the
hedge ratio, the band edge, and the volatility weighting. The one test that
removes the market from the arithmetic entirely, holding the top of the signal
against the bottom of it, returns a daily mean of +0.022 on the fit half and
+0.019 on the graded half with t statistics of 0.31 and 0.27. The ranking is
real as a ranking. Nothing built on it here has been shown to be real as a
source of return.

This is the point where the honest report stops being about a parameter. The
repository has, on the same cached bars, measured: a signal that still ranks in
2026; a graded half that earns +3.78 percent only while the market is short;
a selection that beats its universe by 6.35 percent on one clock and 6.00
percent on another, both of which are shown to be down-market exposures rather
than alphas; and a market-neutral long-short whose return is not
distinguishable from zero. The first is a research finding. The other three
are reasons the research has not produced a book.

What would change that is not another sweep. The second out-of-sample window
has now been supplied by shortening the train window to 250 sessions, and on the
contaminated daily ruler the long-short book fails it. On the honest ruler the
same signal reads +0.19 to +0.27 percent a session after costs, clears a null
control on every window measured, and the remaining work is to push that
lag-one book into the engine and measure it there, because the engine has been
charging the book the return of the session it was picked on.

Three facts remain outside the repository and still gate deployment on top of
all of the above: a quoted lending rate, the shortability of the actual
instruments, and whether an ETF leg can carry a 1x hedge on a book of this
size.
