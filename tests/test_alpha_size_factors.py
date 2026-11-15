"""Tests for the size information set built from float share counts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import polars as pl


def load_module():
    """Import the script without running its CLI."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "alpha_size_factors.py"
    spec = importlib.util.spec_from_file_location("alpha_size_factors", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample_frame() -> pl.DataFrame:
    """Two names over three sessions, with a share issue on the last day."""
    rows = [
        {"code": "A", "datetime": "2024-01-02", "close": 10.0, "volume": 100.0,
         "amount": 1000.0, "float_shares": 1000.0},
        {"code": "A", "datetime": "2024-01-03", "close": 11.0, "volume": 110.0,
         "amount": 1210.0, "float_shares": 1000.0},
        {"code": "A", "datetime": "2024-01-04", "close": 12.0, "volume": 120.0,
         "amount": 1440.0, "float_shares": 1100.0},
        {"code": "B", "datetime": "2024-01-02", "close": 20.0, "volume": 50.0,
         "amount": 1000.0, "float_shares": 500.0},
        {"code": "B", "datetime": "2024-01-03", "close": 19.0, "volume": 50.0,
         "amount": 950.0, "float_shares": 500.0},
        {"code": "B", "datetime": "2024-01-04", "close": 18.0, "volume": 50.0,
         "amount": 900.0, "float_shares": 500.0},
    ]
    return pl.DataFrame(rows).sort(["code", "datetime"])


class TestLoadFloatShares:
    """Reading the per-symbol cache."""

    def test_reads_the_cache(self, tmp_path):
        module = load_module()
        target = tmp_path / "600000_float_shares.csv"
        target.write_text(
            "datetime,float_shares,code\n2024-01-02,29352176848,600000\n"
            "2024-01-03,29352176848,600000\n",
            encoding="utf-8",
        )
        frame = module.load_float_shares(str(tmp_path))
        assert frame.height == 2
        assert frame["code"].to_list() == ["600000", "600000"]
        assert frame["float_shares"][0] == 29352176848.0

    def test_missing_directory_is_empty(self, tmp_path):
        module = load_module()
        frame = module.load_float_shares(str(tmp_path / "nope"))
        assert frame.is_empty()
        assert frame.schema["float_shares"] == pl.Float64

    def test_skips_rows_with_a_bad_share_count(self, tmp_path):
        module = load_module()
        target = tmp_path / "600000_float_shares.csv"
        target.write_text(
            "datetime,float_shares,code\n2024-01-02,0,600000\n"
            "2024-01-03,not-a-number,600000\n"
            "2024-01-04,100,600000\n",
            encoding="utf-8",
        )
        frame = module.load_float_shares(str(tmp_path))
        assert frame.height == 1
        assert frame["datetime"][0] == "2024-01-04"


class TestSizeFeatures:
    """The factor columns."""

    def test_market_cap_is_close_times_shares(self):
        module = load_module()
        out = module.add_size_features(sample_frame())
        row = out.filter(
            (pl.col("code") == "A") & (pl.col("datetime") == "2024-01-03")
        ).row(0, named=True)
        assert row["float_cap"] == 11000.0

    def test_share_change_sees_the_issue(self):
        module = load_module()
        out = module.add_size_features(sample_frame(), windows=(2,))
        row = out.filter(
            (pl.col("code") == "A") & (pl.col("datetime") == "2024-01-04")
        ).row(0, named=True)
        # 1000 -> 1100 shares over two sessions.
        assert abs(row["share_change_2"] - 0.1) < 1e-9

    def test_cap_change_combines_price_and_shares(self):
        module = load_module()
        out = module.add_size_features(sample_frame(), windows=(2,))
        row = out.filter(
            (pl.col("code") == "A") & (pl.col("datetime") == "2024-01-04")
        ).row(0, named=True)
        # Cap went 10000 -> 13200 over two sessions.
        assert abs(row["cap_change_2"] - 0.32) < 1e-9

    def test_cap_rank_is_a_session_relative(self):
        module = load_module()
        out = module.add_size_features(sample_frame())
        last = out.filter(pl.col("datetime") == "2024-01-04")
        a = last.filter(pl.col("code") == "A").row(0, named=True)
        b = last.filter(pl.col("code") == "B").row(0, named=True)
        assert a["float_cap"] > b["float_cap"]
        assert a["cap_pct"] > b["cap_pct"]

    def test_turnover_of_cap_uses_the_same_day_amount(self):
        module = load_module()
        out = module.add_size_features(sample_frame())
        row = out.filter(
            (pl.col("code") == "B") & (pl.col("datetime") == "2024-01-03")
        ).row(0, named=True)
        assert abs(row["turnover_of_cap"] - (950.0 / 9500.0)) < 1e-9

    def test_the_first_window_has_no_value(self):
        module = load_module()
        out = module.add_size_features(sample_frame(), windows=(2,))
        first = out.filter(
            (pl.col("code") == "A") & (pl.col("datetime") == "2024-01-02")
        ).row(0, named=True)
        assert first["cap_change_2"] is None
