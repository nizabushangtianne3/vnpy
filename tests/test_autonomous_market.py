"""Live A-share quote fetching must fail loudly instead of silently.

The tests pin three things the implementation would otherwise get wrong in
production: the market prefix is part of the instrument's identity rather
than something to guess from its digits, prices arrive scaled by 100, and a
batch lookup that cannot resolve one symbol must still return the others.
"""

from __future__ import annotations

import json

import pytest

from vnpy.autonomous import market


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload: dict = payload

    def raise_for_status(self) -> None:  # nothing to raise
        return None

    def json(self) -> dict:
        return self._payload


def _patch_get(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    def fake_get(url: str, params: dict, headers: dict, timeout: float) -> _FakeResponse:
        fake_get.last_url = url  # type: ignore[attr-defined]
        fake_get.last_params = params  # type: ignore[attr-defined]
        return _FakeResponse(payload)

    monkeypatch.setattr(market.requests, "get", staticmethod(fake_get))


def test_secid_keeps_the_exchange_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """000001 is a SZ stock and a SH index; the suffix decides which."""
    assert market.resolve_secid("000001") == "0.000001"
    assert market.resolve_secid("000001.SH") == "1.000001"
    assert market.resolve_secid("600519") == "1.600519"
    assert market.resolve_secid("300750") == "0.300750"
    assert market.resolve_secid("601318.SZSE") == "0.601318"


def test_fetch_quote_scales_price_by_one_hundred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw 141027 must print as 1410.27, not 141027.0."""
    payload: dict = {
        "rc": 0,
        "data": {
            "f43": 141027,
            "f57": "600519",
            "f58": "Moutai",
            "f60": 140230,
            "f86": 1794471112,
            "f169": 797,
            "f170": 57,
        },
    }
    _patch_get(monkeypatch, payload)

    quote: market.Quote = market.fetch_quote("600519")

    assert quote.last_price == pytest.approx(1410.27)
    assert quote.prev_close == pytest.approx(1402.30)
    assert quote.change_pct == pytest.approx(0.57)
    assert quote.change_amount == pytest.approx(7.97)
    assert quote.symbol == "600519"
    assert quote.tradable is True
    assert quote.updated_at


def test_fetch_quote_missing_instrument_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent instrument must surface as an error, not an empty row."""
    _patch_get(monkeypatch, {"rc": 0, "data": None})

    with pytest.raises(ValueError):
        market.fetch_quote("999999")


def test_batch_lookup_keeps_the_symbols_it_could_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One delisted symbol must not swallow the rest of the watchlist."""
    payload: dict = {
        "data": {
            "diff": [
                {
                    "f2": 1179,
                    "f3": 68,
                    "f4": 8,
                    "f12": "000001",
                    "f14": "Ping An Bank",
                    "f18": 1171,
                    "f124": 1794468852,
                }
            ]
        }
    }
    _patch_get(monkeypatch, payload)

    quotes, errors = market.fetch_quotes(["000001", "999999"])

    assert [q.symbol for q in quotes] == ["000001"]
    assert quotes[0].last_price == pytest.approx(11.79)
    assert quotes[0].prev_close == pytest.approx(11.71)
    # The SH index shares the digits 000001; it must stay an error rather
    # than being silently matched to the SZ stock.
    assert {e.symbol for e in errors} == {"999999"}


def test_batch_lookup_distinguishes_same_digits_on_two_exchanges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch holding both 000001 forms must keep both rows distinct."""
    payload: dict = {
        "data": {
            "diff": [
                {
                    "f2": 1179,
                    "f3": 68,
                    "f4": 8,
                    "f12": "000001",
                    "f14": "Ping An Bank",
                    "f18": 1171,
                    "f124": 1794468852,
                },
                {
                    "f2": 365583,
                    "f3": -67,
                    "f4": -2481,
                    "f12": "000001",
                    "f14": "SH Composite",
                    "f18": 368064,
                    "f124": 1794471117,
                },
            ]
        }
    }
    _patch_get(monkeypatch, payload)

    quotes, errors = market.fetch_quotes(["000001", "000001.SH"])

    assert not errors
    assert len(quotes) == 2
    assert {q.secid for q in quotes} == {"0.000001", "1.000001"}
    assert sorted(q.name for q in quotes) == ["Ping An Bank", "SH Composite"]


def test_network_failure_is_retried_then_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient resets should not crash the caller with a raw exception."""
    calls: list[int] = []

    def flaky(url: str, params: dict, headers: dict, timeout: float) -> _FakeResponse:
        calls.append(1)
        raise market.requests.exceptions.ConnectionError("reset")

    monkeypatch.setattr(market.requests, "get", staticmethod(flaky))
    original_sleep = market.time.sleep
    monkeypatch.setattr(market.time, "sleep", lambda _s: None)

    with pytest.raises(ValueError):
        market.fetch_quote("600519")

    assert len(calls) == 3
    monkeypatch.setattr(market.time, "sleep", original_sleep)


def test_cli_quotes_emits_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """--json output must be machine readable for the watch loop."""
    from vnpy.autonomous.__main__ import command_quotes

    payload: dict = {
        "data": {
            "diff": [
                {
                    "f2": 4263,
                    "f3": 16,
                    "f4": 7,
                    "f12": "600036",
                    "f14": "China Merchants Bank",
                    "f18": 4256,
                    "f124": 1794471104,
                }
            ]
        }
    }
    _patch_get(monkeypatch, payload)

    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))

    code: int = command_quotes(
        argparse_namespace(["quotes", "--symbol", "600036", "--json"])
    )

    assert code == 0
    parsed: dict = json.loads(printed[-1])
    assert parsed["quotes"][0]["last_price"] == pytest.approx(42.63)
    assert parsed["errors"] == []


def argparse_namespace(argv: list[str]) -> object:
    """Parse argv the same way the CLI does, to exercise real parsing."""
    from vnpy.autonomous.__main__ import build_parser

    return build_parser().parse_args(argv)
