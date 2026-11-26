"""Grade the book on the prices a real order could actually have received.

The offline ruler credits the book with the open of the session after the
decision, which is the earliest print a lag-one signal can trade against. The
engine's ``DailySpreadStrategy`` instead fills at the bar's own close, because
that is the price a day-level bar makes available on the session it trades. Both
are measurements, but they measure different things, and the gap between them is
the part of the return a real order could not have captured.

This grades four execution assumptions on the identical ranking, the identical
book and the identical cost. Every fill is anchored on the session after the
decision, so the comparison is about which price inside that anchor is used,
never about trading earlier than the signal allows:

* ``open``   -- the session's open, the price reachable in the opening minutes.
* ``close``  -- the session's close, the price the engine's fill uses.
* ``next_open`` -- the open of the session after the anchor, the offline ruler.
* ``next_close`` -- the close of the session after the anchor.

If ``open`` already reproduces the offline reading, the engine's close fill is
merely conservative and the edge needs no advance knowledge of the anchor. If the
reading collapses at ``open``, the return was never in prices a lag-one signal
can reach.

Usage::

    python scripts/alpha_lag1_execprice.py [--json out.json]
"""

from __future__ import annotations

import argparse
import json
from math import erf, sqrt
from pathlib import Path

import numpy as np
import polars as pl

from alpha_lag1_tradable import COST, MIN_LISTED_SESSIONS, MIN_PRICE, MIN_TURNOVER
from alpha_ls_window import find_cache

WINDOWS = [
    ("full", "2022-01-20", "2026-11-13"),
    ("fit", "2024-09-26", "2025-09-30"),
    ("hold", "2025-10-01", "2026-11-13"),
]

YEARS = [
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026", "2026-01-01", "2026-11-13"),
]

# The decision session is index 0, the session the engine actually trades is
# index 1, and the session after that is index 2. The offline ruler buys the
# open of index 1 and sells the open of index 2, so the same-session fills below
# are anchored on index 1 as well, which keeps every assumption inside the span
# the signal is allowed to trade.
ANCHOR: int = 1


def grade(sample: np.ndarray) -> tuple[float, float, float]:
    if len(sample) < 3:
        return float(sample.mean()) if len(sample) else 0.0, float("nan"), float("nan")
    mean = float(sample.mean())
    sd = float(sample.std(ddof=1))
    if sd == 0:
        return mean, 0.0, 1.0
    t = mean / sd * sqrt(len(sample))
    return mean, t, 1.0 - erf(abs(t) / sqrt(2))


def priced_bars(daily: Path) -> pl.DataFrame:
    """Tradable bars shifted so every fill price sits on the anchor session."""
    rows: list[pl.DataFrame] = []
    for path in sorted(daily.glob("*.parquet")):
        frame = pl.read_parquet(
            path,
            columns=["datetime", "open", "high", "low", "close", "volume", "turnover"],
        ).sort("datetime")
        if frame.height < MIN_LISTED_SESSIONS + 8:
            continue
        frame = frame.with_columns(
            pl.int_range(pl.len()).alias("session_index"),
            pl.lit(path.stem).alias("vt_symbol"),
        )
        with_columns = {}
        for label, field in [
            ("open", "open"),
            ("close", "close"),
            ("high", "high"),
            ("low", "low"),
        ]:
            with_columns[f"px_{label}"] = pl.col(field).shift(-ANCHOR)
            with_columns[f"ex_{label}"] = pl.col(field).shift(-(ANCHOR + 1))
        frame = frame.with_columns(**with_columns)
        eligible = (
            (pl.col("session_index") >= MIN_LISTED_SESSIONS)
            & (pl.col("close") >= MIN_PRICE)
            & (pl.col("turnover") >= MIN_TURNOVER)
            & (pl.col("volume") > 0)
            & pl.col("px_open").is_not_null()
            & pl.col("ex_open").is_not_null()
        )
        rows.append(
            frame.filter(eligible).select(
                "datetime", "vt_symbol", *with_columns.keys()
            )
        )
    return pl.concat(rows, how="vertical")


ASSUMPTIONS = [
    ("open→next_open", "open", "open"),
    ("open→same_close", "open", "close"),
    ("close→next_open", "close", "open"),
    ("close→same_close", "close", "close"),
]


def spread(
    joined: pl.DataFrame,
    start: str,
    end: str,
    long_rate: float,
    short_rate: float,
    entry: str,
    exit_: str,
) -> dict:
    """Median spread of the book on one entry and exit price."""
    window = joined.filter(
        (pl.col("datetime") >= pl.lit(start).str.to_datetime())
        & (pl.col("datetime") <= pl.lit(end).str.to_datetime())
    ).sort("datetime")
    groups = {
        d[0] if isinstance(d, tuple) else d: g.sort("signal", descending=True)
        for d, g in window.group_by("datetime")
    }
    spreads: list[float] = []
    shares: list[float] = []
    held_long: set[str] = set()
    held_short: set[str] = set()
    for date in sorted(groups):
        group = groups[date]
        n = group.height
        if n < 300:
            continue
        universe = group["vt_symbol"].to_list()
        long_end = max(int(n * long_rate), 1)
        short_start = n - max(int(n * short_rate), 1)
        new_long = set(universe[:long_end])
        new_short = set(universe[short_start:])
        entered = len(new_long - held_long) + len(new_short - held_short)
        left = len((held_long - new_long) | (held_short - new_short))
        book = (len(new_long) + len(new_short)) or 1
        held_long, held_short = new_long, new_short
        entry_col = group[f"px_{entry}"].to_list()
        exit_col = group[f"ex_{exit_}"].to_list()
        long_rets = [exit_col[i] / entry_col[i] - 1.0 for i in range(long_end)]
        short_rets = [exit_col[i] / entry_col[i] - 1.0 for i in range(short_start, n)]
        long_rets = [x for x in long_rets if x is not None]
        short_rets = [x for x in short_rets if x is not None]
        if len(long_rets) < 5 or len(short_rets) < 5:
            continue
        spreads.append(float(np.median(long_rets)) - float(np.median(short_rets)))
        shares.append((entered + left) / 2.0 / book)
    return {
        "spreads": np.array(spreads),
        "turnover_share": float(np.mean(shares)) if shares else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Execution price assumptions")
    parser.add_argument("--signal", default="")
    parser.add_argument("--long-rate", type=float, default=0.01)
    parser.add_argument("--short-rate", type=float, default=0.01)
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    daily, sweeps = find_cache()
    signal_path = (
        Path(args.signal)
        if args.signal
        else sweeps / "market3000_size_rolling250_250_h5_ens.parquet"
    )
    print("signal:", signal_path, flush=True)
    bars = priced_bars(daily)
    print("priced bars:", bars.height, flush=True)
    signal = pl.read_parquet(signal_path).select("datetime", "vt_symbol", "signal")
    joined = signal.join(bars, on=["datetime", "vt_symbol"], how="inner")
    print(f"band L{int(args.long_rate * 100)}/S{int(args.short_rate * 100)}  join rows: {joined.height}", flush=True)

    report: dict[str, dict] = {}
    print()
    print("可交易宇宙，band 1%，中位数成员，成本 16bp 往返，全部成交锚定在信号后第一根K线")
    print(f"{'window':10}", end="")
    for label, _, _ in ASSUMPTIONS:
        print(f" {label:>20}", end="")
    print()
    for name, start, end in WINDOWS + YEARS:
        print(f"{name:10}", end="", flush=True)
        report[name] = {}
        for label, entry, exit_ in ASSUMPTIONS:
            outcome = spread(
                joined, start, end, args.long_rate, args.short_rate, entry, exit_
            )
            gross = grade(outcome["spreads"])[0]
            turn = outcome["turnover_share"]
            net = gross - turn * COST
            report[name][label] = {
                "gross_bp": round(gross * 10000, 2),
                "net_bp": round(net * 10000, 2),
                "turnover": round(turn, 4),
            }
            print(f" {net * 10000:17.2f}bp", end="", flush=True)
        print()

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("wrote", args.json, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
