"""Tests for the float share fetcher that feeds the size information set."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_module():
    """Import the script without running its CLI."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "alpha_float_shares.py"
    spec = importlib.util.spec_from_file_location("alpha_float_shares", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSecid:
    """The market prefix rule the host expects."""

    def test_shanghai_codes(self):
        module = load_module()
        assert module.secid_for("600000") == "1.600000"
        assert module.secid_for("688981") == "1.688981"

    def test_shenzhen_codes(self):
        module = load_module()
        assert module.secid_for("000001") == "0.000001"
        assert module.secid_for("300750") == "0.300750"


class TestBuildUrl:
    """The request shape."""

    def test_asks_for_turnover_and_float_shares(self):
        module = load_module()
        url = module.build_url("600000", "20240101", "20240131")
        assert "f61" in url
        assert "f70" in url
        assert "secid=1.600000" in url
        assert "beg=20240101" in url

    def test_daily_interval(self):
        module = load_module()
        assert "klt=101" in module.build_url("600000", "20240101", "20240131")

    def test_fallback_routes_are_available(self):
        module = load_module()
        routes = module.build_urls("600000", "20240101", "20240131")
        assert len(routes) == 1 + len(module.PROXY_PREFIXES)
        assert routes[0].startswith("https://push2his.eastmoney.com/")
        for route in routes[1:]:
            assert route.startswith(module.PROXY_PREFIXES)


class TestParseFloatShares:
    """Reading the two extra columns out of a klines row."""

    def test_reads_date_and_shares(self):
        module = load_module()
        rows = module.parse_float_shares(
            [
                "2024-01-02,5.48,5.45,5.50,5.45,220667,0.08,29352176848.00",
                "2024-01-03,5.44,5.49,5.50,5.44,182037,0.06,29352176848.00",
            ]
        )
        assert rows == [
            ("2024-01-02", 29352176848.0),
            ("2024-01-03", 29352176848.0),
        ]

    def test_skips_rows_with_a_blank_share_count(self):
        module = load_module()
        rows = module.parse_float_shares(
            [
                "2024-01-02,5.48,5.45,5.50,5.45,220667,0.08,0",
                "2024-01-03,5.44,5.49,5.50,5.44,182037,0.06,29352176848.00",
            ]
        )
        assert rows == [("2024-01-03", 29352176848.0)]

    def test_skips_malformed_rows(self):
        module = load_module()
        assert module.parse_float_shares(["not a row"]) == []
        assert module.parse_float_shares(["2024-01-02,5.48"]) == []
        assert module.parse_float_shares(None) == []


class TestFetch:
    """The fetch path, exercised without touching the network."""

    def test_reads_the_payload(self):
        module = load_module()
        payload = json.dumps(
            {
                "data": {
                    "klines": [
                        "2024-01-02,5.48,5.45,5.50,5.45,220667,0.08,29352176848.00",
                        "2024-01-03,5.44,5.49,5.50,5.44,182037,0.06,29352176848.00",
                    ]
                }
            }
        ).encode()

        def opener(url: str) -> bytes:
            return payload

        rows = module.fetch_float_shares("600000", "20240101", "20240131", opener=opener)
        assert len(rows) == 2
        assert rows[0][0] == "2024-01-02"

    def test_raises_when_the_host_has_no_data(self):
        module = load_module()

        def opener(url: str) -> bytes:
            return json.dumps({"data": None}).encode()

        try:
            module.fetch_float_shares("600000", "20240101", "20240131", opener=opener)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected a RuntimeError")


class TestCache:
    """The on-disk round trip."""

    def test_write_then_read(self, tmp_path):
        module = load_module()
        target = module.cache_path(tmp_path, "600000")
        module.write_rows(target, [("2024-01-02", 29352176848.0)])
        assert module.read_rows(target) == [("2024-01-02", 29352176848.0)]

    def test_read_of_a_missing_file_is_empty(self, tmp_path):
        module = load_module()
        assert module.read_rows(tmp_path / "nope.csv") == []
