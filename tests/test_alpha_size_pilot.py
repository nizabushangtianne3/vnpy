"""Tests for the controlled size pilot."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def load_module():
    """Import the script without running its CLI."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "alpha_size_pilot.py"
    spec = importlib.util.spec_from_file_location("alpha_size_pilot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCoveredCodes:
    """Finding the names that have landed."""

    def test_reads_the_cache_directory(self, tmp_path):
        module = load_module()
        (tmp_path / "000001_float_shares.csv").write_text("x", encoding="utf-8")
        (tmp_path / "600000_float_shares.csv").write_text("x", encoding="utf-8")
        (tmp_path / "not_a_symbol.txt").write_text("x", encoding="utf-8")
        codes = module.covered_codes(str(tmp_path))
        assert codes == ["000001", "600000"]

    def test_missing_directory_is_empty(self, tmp_path):
        module = load_module()
        assert module.covered_codes(str(tmp_path / "nope")) == []
