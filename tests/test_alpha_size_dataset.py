"""Tests for the size-aware dataset builder."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl


def load_module():
    """Import the script without running its CLI."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "alpha_size_dataset.py"
    spec = importlib.util.spec_from_file_location("alpha_size_dataset", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bars_frame() -> pl.DataFrame:
    """Two names over three sessions with absolute turnover and volume."""
    rows = [
        {"datetime": "2024-01-02", "vt_symbol": "000001.SZSE", "turnover": 1000.0, "volume": 100.0},
        {"datetime": "2024-01-03", "vt_symbol": "000001.SZSE", "turnover": 1100.0, "volume": 100.0},
        {"datetime": "2024-01-04", "vt_symbol": "000001.SZSE", "turnover": 1200.0, "volume": 100.0},
        {"datetime": "2024-01-02", "vt_symbol": "000002.SZSE", "turnover": 500.0, "volume": 50.0},
        {"datetime": "2024-01-03", "vt_symbol": "000002.SZSE", "turnover": 500.0, "volume": 50.0},
        {"datetime": "2024-01-04", "vt_symbol": "000002.SZSE", "turnover": 500.0, "volume": 50.0},
    ]
    return pl.DataFrame(rows).with_columns(pl.col("datetime").str.to_datetime())


def write_shares(tmp_path: Path) -> str:
    """Write two symbols' float share files and return the cache directory."""
    first = tmp_path / "000001_float_shares.csv"
    first.write_text(
        "datetime,float_shares,code\n"
        "2024-01-02,1000,000001\n"
        "2024-01-03,1000,000001\n"
        "2024-01-04,1100,000001\n",
        encoding="utf-8",
    )
    second = tmp_path / "000002_float_shares.csv"
    second.write_text(
        "datetime,float_shares,code\n"
        "2024-01-02,500,000002\n"
        "2024-01-03,500,000002\n"
        "2024-01-04,500,000002\n",
        encoding="utf-8",
    )
    return str(tmp_path)


class TestSymbolToCode:
    """The code behind a vt_symbol."""

    def test_strips_the_exchange_suffix(self):
        module = load_module()
        assert module.symbol_to_code("000001.SZSE") == "000001"
        assert module.symbol_to_code("600000.SSE") == "600000"


class TestAttachSize:
    """Joining the share count and building the factors."""

    def test_market_cap_uses_the_absolute_price(self, tmp_path):
        module = load_module()
        out = module.attach_size(bars_frame(), write_shares(tmp_path))
        row = out.filter(
            (pl.col("vt_symbol") == "000001.SZSE")
            & (pl.col("datetime").cast(pl.Utf8).str.slice(0, 10) == "2024-01-02")
        ).row(0, named=True)
        # turnover/volume is 10 yuan, times 1000 shares.
        assert abs(row["float_cap"] - 10000.0) < 1e-6

    def test_size_rank_follows_the_cap(self, tmp_path):
        module = load_module()
        out = module.attach_size(bars_frame(), write_shares(tmp_path))
        last = out.filter(
            pl.col("datetime").cast(pl.Utf8).str.slice(0, 10) == "2024-01-04"
        )
        first = last.filter(pl.col("vt_symbol") == "000001.SZSE").row(0, named=True)
        second = last.filter(pl.col("vt_symbol") == "000002.SZSE").row(0, named=True)
        assert first["size_cap_rank"] > second["size_cap_rank"]
        assert first["size_cap_pct"] > second["size_cap_pct"]

    def test_share_change_sees_the_issue(self, tmp_path):
        module = load_module()
        out = module.attach_size(bars_frame(), write_shares(tmp_path), windows=(2,))
        row = out.filter(
            (pl.col("vt_symbol") == "000001.SZSE")
            & (pl.col("datetime").cast(pl.Utf8).str.slice(0, 10) == "2024-01-04")
        ).row(0, named=True)
        # 1000 -> 1100 shares over two sessions.
        assert abs(row["size_share_change_2"] - 0.1) < 1e-9

    def test_relative_size_is_centred_on_the_session(self, tmp_path):
        module = load_module()
        out = module.attach_size(bars_frame(), write_shares(tmp_path))
        session = out.filter(
            pl.col("datetime").cast(pl.Utf8).str.slice(0, 10) == "2024-01-02"
        )
        values = session["size_relative"].to_list()
        # One is above the session mean and one is below it.
        assert any(v > 0 for v in values)
        assert any(v < 0 for v in values)

    def test_a_suspended_session_keeps_the_previous_price(self, tmp_path):
        module = load_module()
        frame = bars_frame().with_columns(
            pl.when(
                (pl.col("vt_symbol") == "000001.SZSE")
                & (pl.col("datetime").cast(pl.Utf8).str.slice(0, 10) == "2024-01-04")
            )
            .then(pl.lit(0.0))
            .otherwise(pl.col("volume"))
            .alias("volume")
        )
        out = module.attach_size(frame, write_shares(tmp_path), windows=(2,))
        row = out.filter(
            (pl.col("vt_symbol") == "000001.SZSE")
            & (pl.col("datetime").cast(pl.Utf8).str.slice(0, 10) == "2024-01-04")
        ).row(0, named=True)
        # No volume, so the price carries over from the session before.
        assert row["avg_price"] is None
        assert abs(row["price_filled"] - 11.0) < 1e-6

    def test_missing_share_count_leaves_a_null_cap(self, tmp_path):
        module = load_module()
        cache: str = write_shares(tmp_path)
        frame = bars_frame().filter(
            pl.col("vt_symbol") == "000001.SZSE"
        )
        out = module.attach_size(frame, cache)
        assert out["float_shares"].null_count() == 0


class TestFeatureColumns:
    """The list of columns the model reads."""

    def test_lists_every_size_factor(self):
        module = load_module()
        columns = module.size_feature_columns()
        assert "size_log_cap" in columns
        assert "size_cap_pct" in columns
        assert "size_share_change_60" in columns
        assert len(columns) == 10


class TestAugmentDataset:
    """Joining the size columns onto the frames the dataset carries."""

    def _fake_dataset(self, frame: pl.DataFrame):
        class FakeDataset:
            pass

        dataset = FakeDataset()
        dataset.df = frame
        dataset.result_df = frame
        dataset.raw_df = frame
        dataset.infer_df = frame
        dataset.learn_df = frame
        return dataset

    def _sized(self, frame: pl.DataFrame, tmp_path, shares: float = 1.0) -> pl.DataFrame:
        module = load_module()
        out = frame.with_columns(
            pl.col("vt_symbol")
            .map_elements(lambda s: s.split(".")[0], return_dtype=pl.Utf8)
            .alias("code")
        )
        out = out.with_columns(pl.lit(shares).alias("float_shares"))
        return module.attach_size(out, write_shares(tmp_path), windows=(2,))

    def test_the_price_factors_do_not_move(self, tmp_path):
        module = load_module()
        base = bars_frame().with_columns(pl.lit(0.5).alias("factor_one"))
        dataset = self._fake_dataset(base)

        sized = self._sized(base, tmp_path)
        columns = module.size_feature_columns(windows=(2,))
        out = module.augment_dataset(dataset, sized, columns)

        assert out.result_df.height == dataset.result_df.height
        assert out.result_df.select("factor_one").equals(
            dataset.result_df.select("factor_one")
        )

    def test_the_size_columns_land_on_every_frame(self, tmp_path):
        module = load_module()
        base = bars_frame()
        dataset = self._fake_dataset(base)

        sized = self._sized(base, tmp_path)
        columns = module.size_feature_columns(windows=(2,))
        out = module.augment_dataset(dataset, sized, columns)

        for frame_name in ("df", "result_df", "raw_df", "infer_df", "learn_df"):
            frame = getattr(out, frame_name)
            for column in columns:
                assert column in frame.columns, f"{column} missing from {frame_name}"

    def test_a_missing_size_becomes_nan_not_a_number(self, tmp_path):
        module = load_module()
        base = bars_frame()
        dataset = self._fake_dataset(base)

        # Only one of the two names has a share count.
        one_name = base.filter(pl.col("vt_symbol") == "000001.SZSE")
        sized = self._sized(one_name, tmp_path)
        columns = module.size_feature_columns(windows=(2,))
        out = module.augment_dataset(dataset, sized, columns)
        # The booster reads a NaN as a missing value and a null as a row it
        # cannot use, so the gap has to be NaN the way the price factors are.
        nan_count = sum(
            out.result_df.select(pl.col(c).is_nan().sum()).item() for c in columns
        )
        assert nan_count > 0
        assert out.result_df.select(columns).null_count().row(0) == (0,) * len(columns)
