# Alpha pilot findings

## The result that looked good was a misalignment

`scripts/alpha_pilot.py` used to attach the model's predictions to an unsorted
inference frame: `model.predict` sorts by `datetime, vt_symbol` before building
its input matrix, while the frame it was joined onto was not sorted. The first
row of the unsorted frame is `600009.SSE`, the first sorted row is `600000.SSE`,
so every prediction landed on a neighbour's symbol. The book was then ranked
and traded on signals that belonged to other stocks.

While that misalignment was in place, a 5-session rebalance read as 12.98
percent total return with 53x capital turnover and 5.29 percent of capital in
commissions. Once the order is aligned, the same configuration returns 1.06
percent with 4.67x turnover and 0.52 percent of capital in commissions. The
12.98 percent was largely turnover on mismatched names, not edge.

Both `scripts/alpha_pilot.py` and `scripts/run_alpha_campaign.py` now sort the
inference frame before attaching the predictions.

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
