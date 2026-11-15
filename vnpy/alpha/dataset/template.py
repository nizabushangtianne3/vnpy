"""
Dataset template that calculates expressions and serves train, valid, and test segments.
"""

import time
from datetime import datetime
from typing import cast
from collections.abc import Callable
from multiprocessing import get_context
from multiprocessing.context import BaseContext
from multiprocessing.pool import IMapIterator, Pool

import polars as pl
import pandas as pd
from tqdm import tqdm

from ..logger import logger
from .factor_performance import (
    compute_factor_metrics,
    create_full_tear_sheet,
    prepare_factor_data,
)
from .utility import (
    to_datetime,
    Segment,
    calculate_by_expression,
    calculate_by_polars
)


class AlphaDataset:
    """Alpha dataset template class"""

    def __init__(
        self,
        df: pl.DataFrame,
        train_period: tuple[str, str],
        valid_period: tuple[str, str],
        test_period: tuple[str, str],
        process_type: str = "append"
    ) -> None:
        """Constructor"""
        self.df: pl.DataFrame = df

        # DataFrames for processed data
        self.result_df: pl.DataFrame
        self.raw_df: pl.DataFrame
        self.infer_df: pl.DataFrame
        self.learn_df: pl.DataFrame

        # New version
        self.data_periods: dict[Segment, tuple[str, str]] = {
            Segment.TRAIN: train_period,
            Segment.VALID: valid_period,
            Segment.TEST: test_period
        }

        self.feature_expressions: dict[str, str | pl.expr.expr.Expr] = {}
        self.feature_results: dict[str, pl.DataFrame] = {}
        self.label_expression: str = ""

        self.process_type: str = process_type
        self.infer_processors: list = []
        self.learn_processors: list = []

    def add_feature(
        self,
        name: str,
        expression: str | pl.expr.expr.Expr | None = None,
        result: pl.DataFrame | None = None
    ) -> None:
        """
        Add a feature expression
        """
        if expression is not None and result is not None:
            raise ValueError("Only one of 'expression' or 'result' can be provided")

        if expression is not None:
            self.feature_expressions[name] = expression
        elif result is not None:
            self.feature_results[name] = result

    def set_label(self, expression: str) -> None:
        """
        Set the label expression
        """
        self.label_expression = expression

    def add_processor(self, task: str, processor: Callable[[pl.DataFrame], None]) -> None:
        """
        Add a feature preprocessor
        """
        if task == "infer":
            self.infer_processors.append(processor)
        else:
            self.learn_processors.append(processor)

    def prepare_data(self, filters: dict | None = None, max_workers: int | None = None) -> None:
        """
        Generate required data
        """
        # List for feature data results
        results: list = []

        # Iterate through expressions for calculation
        expressions: list[tuple[str, str | pl.expr.expr.Expr]] = list(self.feature_expressions.items())

        if self.label_expression:
            expressions.append(("label", self.label_expression))

        # Create process pool
        logger.info("开始计算表达式因子特征")

        args: list[tuple] = [(self.df, name, expression) for name, expression in expressions]

        # Every task pickles the whole frame, so a pool of N workers serialises
        # N copies of it before the first expression runs. On a market-sized
        # frame that is more data than the machine will move, and the pool
        # stalls without producing a factor. A single worker pays the cost
        # once instead of N times, which is why max_workers=1 is a real option
        # and not merely a degenerate one.
        if max_workers == 1:
            for arg in tqdm(args, total=len(args)):
                results.append(calculate_feature(arg))
        else:
            context: BaseContext = get_context("spawn")

            pool: Pool
            with context.Pool(processes=max_workers) as pool:
                # Calculate all expressions in parallel
                it: IMapIterator[pl.Series] = pool.imap(calculate_feature, args)

                # Collect results
                result: pl.Series
                for result in tqdm(it, total=len(args)):
                    results.append(result)

        self.result_df = self.df.with_columns(results)

        # Each factor series is copied into the frame here, and the list keeps
        # holding its own reference to all of them afterwards. On a
        # market-sized frame that dead list is as large as the frame itself, and
        # every later stage pays for it: the fill, the filter, the concat and
        # the sort. Dropping the references as soon as the columns are attached
        # removes that copy without changing a single value.
        results.clear()

        # Merge result data factor features
        logger.info("开始合并结果数据因子特征")

        label_exist: bool = "label" in self.result_df
        name: str
        feature_result: pl.DataFrame
        for name, feature_result in tqdm(self.feature_results.items()):
            feature_result = feature_result.rename({"data": name})
            self.result_df = self.result_df.join(feature_result, on=["datetime", "vt_symbol"], how="left")

        if label_exist:
            # Put label at the last column
            cols: list = [col for col in self.result_df.columns if col != "label"] + ["label"]
            self.result_df = self.result_df.select(cols).sort(["datetime", "vt_symbol"])

        # Only the factors are read by the booster, and it reads them as 32-bit
        # floats whatever the frame says. Holding them at 64 bits doubles every
        # copy a market-sized build takes of this frame -- the result frame
        # itself, the filled copy, each filtered slice, their concatenation and
        # the sort that follows it -- and that product, not the arithmetic, is
        # what a full-market run runs out of memory on. The price columns keep
        # their 64 bits because the label is recomputed from them, and a
        # target rounded before the model sees it is a target that moved.
        factor_names: list = [
            name for name in self.result_df.columns[self.df.width :] if name != "label"
        ]
        if factor_names:
            self.result_df = self.result_df.with_columns(
                [pl.col(name).cast(pl.Float32) for name in factor_names]
            )

        # The filter only reads `vt_symbol` and `datetime`, and `fill_null` is
        # applied to the surviving rows below. Filling first would push every
        # null factor of every symbol to NaN across the whole market frame
        # before a single row is dropped, which is a second full-size copy that
        # the component filter then throws most of away.
        raw_df: pl.DataFrame = self.result_df

        if filters:
            logger.info("开始筛选成分股数据")
            vt_symbol: str
            ranges: list[tuple[datetime, datetime]]
            # A symbol's spans are contiguous runs, so grouping by the run itself
            # turns thousands of per-symbol scans into one scan per run: the
            # frame is visited once per interval instead of once per symbol,
            # and every row that survives is identical to the old per-symbol
            # union.
            span_symbols: dict[tuple[datetime, datetime], list[str]] = {}
            for vt_symbol, ranges in filters.items():
                for start, end in ranges:
                    span_symbols.setdefault((start, end), []).append(vt_symbol)

            # Filtering with `filter` materialises one frame per span, and a
            # market-sized build has thousands of them, so the surviving rows
            # are held twice over before the concat even starts. Building the
            # boolean keep-mask instead and gathering once in the end touches
            # each row a single time and never holds two copies of the frame.
            keep: pl.Series | None = None
            for (span_start, span_end), symbols in tqdm(
                span_symbols.items(), total=len(span_symbols)
            ):
                inside: pl.Series = raw_df.select(
                    pl.col("vt_symbol").is_in(symbols)
                    & (pl.col("datetime") >= pl.lit(span_start))
                    & (pl.col("datetime") <= pl.lit(span_end))
                ).to_series()
                keep = inside if keep is None else (keep | inside)

            if keep is not None:
                raw_df = raw_df.gather(pl.arg_where(keep)).sort(["datetime", "vt_symbol"])
            del keep

        # Only keep feature columns
        select_columns: list[str] = ["datetime", "vt_symbol"] + raw_df.columns[self.df.width:]
        # Fill after the filter, so the null-to-NaN pass runs over the rows that
        # are kept rather than over the whole market frame it is cut from.
        self.raw_df = raw_df.select(select_columns).fill_null(float("nan"))

        self.infer_df = self.raw_df
        self.learn_df = self.raw_df

    def process_data(self) -> None:
        """
        Process data
        """
        # Generate inference data
        processor: Callable[..., pl.DataFrame]
        for processor in self.infer_processors:
            self.infer_df = processor(df=self.infer_df)

        # Generate learning data
        if self.process_type == "append":
            self.learn_df = self.infer_df

        for processor in self.learn_processors:
            self.learn_df = processor(df=self.learn_df)

    def fetch_raw(self, segment: Segment) -> pl.DataFrame:
        """
        Get raw data for a specific segment
        """
        start: str
        end: str
        start, end = self.data_periods[segment]
        return query_by_time(self.raw_df, start, end)

    def fetch_infer(self, segment: Segment) -> pl.DataFrame:
        """
        Get inference data for a specific segment
        """
        start: str
        end: str
        start, end = self.data_periods[segment]
        return query_by_time(self.infer_df, start, end)

    def fetch_learn(self, segment: Segment) -> pl.DataFrame:
        """
        Get learning data for a specific segment
        """
        start: str
        end: str
        start, end = self.data_periods[segment]
        return query_by_time(self.learn_df, start, end)

    def show_feature_performance(self, name: str) -> None:
        """
        Perform performance analysis for a feature
        """
        starts: list[datetime] = []
        ends: list[datetime] = []

        period: tuple[str, str]
        for period in self.data_periods.values():
            starts.append(to_datetime(period[0]))
            ends.append(to_datetime(period[1]))

        start: datetime = min(starts)
        end: datetime = max(ends)

        # Select range
        result_df: pl.DataFrame = query_by_time(self.result_df, start, end)
        learn_df: pl.DataFrame = query_by_time(self.learn_df, start, end)

        merged_df: pl.DataFrame = (
            result_df
            .select(["datetime", "vt_symbol", "close"])
            .join(
                learn_df.select(["datetime", "vt_symbol", name]),
                on=["datetime", "vt_symbol"],
                how="inner"
            )
        )

        # Fill NaN and drop nulls
        merged_df = merged_df.fill_nan(None).drop_nulls()

        # Extract feature
        feature_df: pd.DataFrame = merged_df.select(["datetime", "vt_symbol", name]).to_pandas()
        feature_df.set_index(["datetime", "vt_symbol"], inplace=True)

        feature_s: pd.Series = feature_df[name]

        label_df: pd.DataFrame = result_df.select(["datetime", "vt_symbol", "label"]).to_pandas()
        label_df.set_index(["datetime", "vt_symbol"], inplace=True)
        label_s: pd.Series = label_df["label"]

        # Extract price
        price_df: pd.DataFrame = result_df.select(["datetime", "vt_symbol", "close"]).to_pandas()
        price_df = price_df.pivot(index="datetime", columns="vt_symbol", values="close")

        # Merge data
        clean_data: pd.DataFrame = prepare_factor_data(
            feature_s,
            label_s,
            price_df,
            quantiles=10,
        )

        # Perform analysis
        metrics = compute_factor_metrics(clean_data, self.label_expression)
        create_full_tear_sheet(metrics, name).show()

    def show_signal_performance(self, signal: pl.DataFrame) -> None:
        """
        Perform performance analysis for prediction signals
        """
        # Get signal start and end times
        start: datetime = cast(datetime, signal["datetime"].min())
        end: datetime = cast(datetime, signal["datetime"].max())

        # Select range
        df: pl.DataFrame = query_by_time(self.result_df, start, end)

        # Extract feature
        signal_df: pd.DataFrame = signal.to_pandas()
        signal_df.set_index(["datetime", "vt_symbol"], inplace=True)
        signal_s: pd.Series = signal_df["signal"]

        label_df: pd.DataFrame = df.select(["datetime", "vt_symbol", "label"]).to_pandas()
        label_df.set_index(["datetime", "vt_symbol"], inplace=True)
        label_s: pd.Series = label_df["label"]

        # Extract price
        price_df: pd.DataFrame = df.select(["datetime", "vt_symbol", "close"]).to_pandas()
        price_df = price_df.pivot(index="datetime", columns="vt_symbol", values="close")

        # Merge data
        clean_data: pd.DataFrame = prepare_factor_data(
            signal_s,
            label_s,
            price_df,
            max_loss=1.0,
            quantiles=10
        )

        # Perform analysis
        metrics = compute_factor_metrics(clean_data, self.label_expression)
        create_full_tear_sheet(metrics, "Signal").show()


def query_by_time(df: pl.DataFrame, start: datetime | str = "", end: datetime | str = "") -> pl.DataFrame:
    """
    Filter DataFrame based on time range
    """
    if start:
        start = to_datetime(start)
        df = df.filter(pl.col("datetime") >= start)

    if end:
        end = to_datetime(end)
        df = df.filter(pl.col("datetime") <= end)

    return df.sort(["datetime", "vt_symbol"])


def calculate_feature(args: tuple[pl.DataFrame, str, str | pl.expr.expr.Expr]) -> pl.Series:
    """
    Calculate feature by expression
    """
    start: float = time.time()

    df: pl.DataFrame
    name: str
    expression: str | pl.expr.expr.Expr
    df, name, expression = args

    if isinstance(expression, pl.expr.expr.Expr):
        result: pl.Series = calculate_by_polars(df, expression)["data"].alias(name)
    else:
        result = calculate_by_expression(df, expression)["data"].alias(name)

    end: float = time.time()
    print(f"Feature calculation {name} took: {end - start} seconds | {expression}")

    return result
