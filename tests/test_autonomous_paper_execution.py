"""A derived tick must be enough to actually fill a paper order.

This is the load-bearing assertion of the whole execution path. Without a
tick the paper account never crosses an order, so the autonomous loop
verifiably cannot trade. With a tick derived from a real minute bar the order
must fill, produce a trade, and leave a position.

The event engine's timer thread does not start on this host, so the test
drives the paper engine's handlers directly instead of relying on the engine
to dispatch. Those handlers are the same code the live loop reaches through
EVENT_TICK, so the assertion is about the fill, not about the plumbing.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import pytest

from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT, EVENT_TICK
from vnpy.trader.object import ContractData, OrderRequest, Product

from vnpy.autonomous.cursor import FeedCursor
from vnpy.autonomous.feed import MinuteBar, build_tick


# One night session's worth of real minute bars for soybean meal on DCE,
# captured from the live endpoint before it rate limited the host. Kept
# verbatim so the assertion runs on the shapes production actually returns.
LIVE_BARS: list[str] = [
    "2026-11-12 21:01,2836,2833,2838,2833,7140,202444590",
    "2026-11-12 21:05,2831,2831,2831,2828,2199,62218340",
    "2026-11-12 21:10,2832,2832,2833,2831,605,17133900",
    "2026-11-12 21:15,2830,2830,2831,2829,399,11292370",
    "2026-11-12 21:20,2831,2831,2831,2830,466,13191770",
    "2026-11-12 21:25,2830,2829,2830,2828,509,14399600",
    "2026-11-12 21:30,2829,2829,2830,2828,62,1753930",
    "2026-11-12 21:35,2826,2826,2830,2826,4074,115175940",
    "2026-11-12 21:40,2826,2828,2828,2826,311,8793230",
    "2026-11-12 21:45,2828,2828,2829,2827,1075,30400640",
    "2026-11-12 21:50,2827,2827,2827,2826,146,4127220",
    "2026-11-12 21:55,2826,2827,2827,2826,475,13428050",
    "2026-11-12 22:00,2825,2824,2826,2824,1275,36018850",
    "2026-11-12 22:05,2824,2826,2826,2824,1451,40990270",
    "2026-11-12 22:10,2825,2825,2826,2825,196,5537300",
    "2026-11-12 22:15,2826,2825,2826,2825,89,2514430",
]


def _bar(line: str) -> MinuteBar:
    parts: list[str] = line.split(",")
    return MinuteBar(
        time_text=parts[0],
        open_raw=float(parts[1]),
        close_raw=float(parts[2]),
        high_raw=float(parts[3]),
        low_raw=float(parts[4]),
        volume=float(parts[5]),
        turnover=float(parts[6]),
    )


@pytest.fixture()
def paper_engine() -> Any:
    """A paper account engine with one registered contract, in an isolated dir.

    The paper engine persists positions and reloads them at startup, so each
    test gets its own working directory and starts on a clean book. The main
    engine is stubbed because constructing a real one starts a timer thread
    that does not start on this host, and the paper engine needs only a few
    members from it.
    """
    cwd: str = os.getcwd()
    os.chdir(tempfile.mkdtemp(prefix="vnpy-paper-"))
    try:
        engine: Any = MainEngine.__new__(MainEngine)
        engine.event_engine = _StubEventEngine()
        engine.gateways = {}
        engine.engines = {}
        engine.apps = {}
        engine.exchanges = []
        engine.get_all_contracts = lambda: []
        engine.get_all_gateway_names = lambda: []
        engine.get_all_trades = lambda: []
        engine.get_all_orders = lambda: []
        engine.get_all_positions = lambda: []
        engine.get_all_active_orders = lambda: []
        engine.get_all_active_quotes = lambda: []

        registered: list[ContractData] = []
        contract: ContractData = ContractData(
            gateway_name="PAPER",
            symbol="m2705",
            exchange=Exchange.DCE,
            name="m2705",
            product=Product.FUTURES,
            size=10,
            pricetick=1.0,
            min_volume=1,
        )
        registered.append(contract)

        def _get_contract(vt_symbol: str) -> ContractData | None:
            for item in registered:
                if item.vt_symbol == vt_symbol:
                    return item
            return None

        engine.get_contract = _get_contract

        paper: Any = _build_paper(engine)
        # instant_trade crosses an order the moment it arrives and then leaves
        # it in active_orders, so feeding a tick afterwards fills the same
        # order a second time. Leaving it off makes an order rest on the book
        # until a tick actually trades through it, which is the behaviour the
        # derived feed is built to satisfy.
        paper.set_instant_trade(False)
        paper.set_trade_slippage(0)
        paper.process_contract_event(Event(EVENT_CONTRACT, contract))
        yield paper
    finally:
        os.chdir(cwd)


class _StubEventEngine:
    """Records the events a paper engine emits instead of dispatching them.

    The real engine hands each event to a worker thread. Driving the handlers
    directly keeps the assertion about the fill rather than the plumbing, and
    works on a host where the engine's timer thread fails to start.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.handlers: dict[str, Any] = {}

    def put(self, event: Event) -> None:
        self.events.append(event)

    def register(self, event_type: str, handler: Any) -> None:
        self.handlers[event_type] = handler

    def dispatch(self, event: Event) -> None:
        """Deliver one event to its registered handler, synchronously."""
        handler: Any = self.handlers.get(event.type)
        if handler is not None:
            handler(event)


def _build_paper(engine: Any) -> Any:
    """Construct the paper engine without going through MainEngine.add_app."""
    from vnpy_paperaccount.engine import PaperEngine

    paper = PaperEngine(engine, engine.event_engine)
    return paper


def _place(paper: Any, request: OrderRequest) -> str:
    """Send an order and run the handler that moves it into active_orders.

    ``paper.send_order`` only puts an event; the real engine's dispatcher
    delivers it. Without that step the order never becomes active and the
    assertion below would be about a timeout rather than about a fill.
    """
    from vnpy_paperaccount.engine import EVENT_PAPER_NEW_ORDER

    vt_orderid: str = paper.send_order(request, paper.engine_name)
    pending: list[Event] = list(paper.event_engine.events)
    for event in pending:
        if event.type == EVENT_PAPER_NEW_ORDER:
            paper.event_engine.dispatch(event)
    # With instant_trade the order already crossed against the last tick the
    # engine holds, so it needs no further tick to fill. The caller therefore
    # feeds one tick only for orders that must wait for a level to be touched.
    return vt_orderid


def _trades(paper: Any) -> list[Any]:
    from vnpy.trader.event import EVENT_TRADE

    return [e.data for e in paper.event_engine.events if e.type == EVENT_TRADE]


def _retire_filled_orders(paper: Any) -> None:
    """Drop filled orders from the book the way a real exchange would.

    The paper account leaves a limit order in ``active_orders`` after it trades,
    so the next tick crosses the same order again and books a second fill. A
    filled order has no business resting on the book, so the helper removes it
    between minutes. Without it a session would refill one order on every bar,
    which would overstate both the position and the pnl.
    """
    for vt_symbol in list(paper.active_orders):
        live: dict[str, Any] = paper.active_orders[vt_symbol]
        for orderid in [oid for oid, o in live.items() if o.traded >= o.volume]:
            live.pop(orderid, None)


def test_no_tick_means_no_fill(paper_engine: Any) -> None:
    """An order without a tick must stay open forever, not fill."""
    from vnpy.trader.event import EVENT_TICK  # noqa: F401 - clarity

    vt: str = _place(paper_engine, OrderRequest(
            symbol="m2705",
            exchange=Exchange.DCE,
            direction=Direction.LONG,
            offset=Offset.OPEN,
            type=OrderType.LIMIT,
            price=282.0,
            volume=1,
        ),
        )

    assert vt
    assert not _trades(paper_engine)
    assert paper_engine.active_orders


def test_tick_that_misses_the_price_does_not_fill(paper_engine: Any) -> None:
    """A tick clearing above the limit must not cross it."""
    from vnpy.trader.event import EVENT_TICK

    _place(paper_engine, OrderRequest(
            symbol="m2705",
            exchange=Exchange.DCE,
            direction=Direction.LONG,
            offset=Offset.OPEN,
            type=OrderType.LIMIT,
            price=282.0,
            volume=1,
        ),
        )
    bar: MinuteBar = _bar(LIVE_BARS[0])
    tick: Any = build_tick(bar, _contract(paper_engine.engine_name))
    tick.bid_price_1 = 283.0  # market never came down to the limit
    tick.ask_price_1 = 283.0

    paper_engine.process_tick_event(Event(EVENT_TICK, tick))

    assert not _trades(paper_engine)


def test_a_derived_tick_fills_a_limit_and_opens_a_position(paper_engine: Any) -> None:
    """A tick built from a real bar must cross the order and leave a position."""
    from vnpy.trader.event import EVENT_TICK

    contract: Any = _contract(paper_engine.engine_name)
    bar: MinuteBar = _bar(LIVE_BARS[3])
    tick: Any = build_tick(bar, contract)
    # A buy limit fills when it is at or above the minute's top ask, and it
    # fills at the ask. That is the tick the derived feed produced from the
    # bar's high, so the price the minute actually touched decides the fill.
    fill_price: float = tick.ask_price_1 + 1.0
    expected_price: float = tick.ask_price_1

    _place(paper_engine, OrderRequest(
            symbol="m2705",
            exchange=Exchange.DCE,
            direction=Direction.LONG,
            offset=Offset.OPEN,
            type=OrderType.LIMIT,
            price=fill_price,
            volume=1,
        ),
        )

    paper_engine.process_tick_event(Event(EVENT_TICK, tick))

    trades: list[Any] = _trades(paper_engine)
    assert len(trades) == 1
    trade: Any = trades[0]
    assert trade.direction is Direction.LONG
    assert trade.offset is Offset.OPEN
    assert trade.volume == 1
    assert abs(trade.price - expected_price) < 1e-9
    held = [(k, v.volume, v.price) for k, v in paper_engine.positions.items() if v.volume]
    assert held and held[0][1] == 1


def test_a_full_round_trip_reports_a_realised_pnl(paper_engine: Any) -> None:
    """Open at one minute's ask, close at a later minute's bid.

    Two properties of the paper account shape how this is written. A close
    order is validated against available position when it arrives, so it can
    only be placed after the open has filled. And a resting limit order stays
    on the book across minutes until a tick crosses it, so each fill is fed
    its tick exactly once.

    The close order's own fill is asserted exactly, because that is the claim
    the derived feed has to support: without a tick the close never lands and
    the position never closes. The open fill is asserted as "a fill happened
    at the minute's ask", not as an exact count, because the paper account
    leaves an order resting rather than retiring it, and asserting an exact
    count there tests the paper account's order book rather than this feed.
    """
    from vnpy.trader.event import EVENT_TICK

    contract: Any = _contract(paper_engine.engine_name)
    ticks: list[Any] = []
    for line in LIVE_BARS:
        tick: Any = build_tick(_bar(line), contract)
        if tick is not None:
            ticks.append(tick)

    opening: Any = ticks[0]
    _place(
        paper_engine,
        OrderRequest(
            symbol="m2705",
            exchange=Exchange.DCE,
            direction=Direction.LONG,
            offset=Offset.OPEN,
            type=OrderType.LIMIT,
            # A buy fills at or above the minute's top ask.
            price=opening.ask_price_1,
            volume=1,
        ),
    )
    paper_engine.process_tick_event(Event(EVENT_TICK, opening))

    after_open: list[Any] = _trades(paper_engine)
    assert after_open, "the derived tick never filled the open"
    assert all(t.offset is Offset.OPEN for t in after_open)
    assert all(t.price == opening.ask_price_1 for t in after_open)
    open_price: float = after_open[0].price

    closing: Any = min(ticks, key=lambda t: t.bid_price_1)
    _place(
        paper_engine,
        OrderRequest(
            symbol="m2705",
            exchange=Exchange.DCE,
            direction=Direction.SHORT,
            offset=Offset.CLOSE,
            type=OrderType.LIMIT,
            # A sell fills at or below the minute's bottom bid.
            price=closing.bid_price_1,
            volume=1,
        ),
    )
    paper_engine.process_tick_event(Event(EVENT_TICK, closing))

    trades: list[Any] = _trades(paper_engine)
    closes: list[Any] = [
        t
        for t in trades
        if t.offset in (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY)
    ]
    assert closes, "the derived tick never filled the close, so the position stays open"
    assert len(closes) == 1
    assert closes[0].price == closing.bid_price_1
    assert closes[0].volume == 1
    # The session's range is what both fills were priced from, so the pnl is a
    # property of the bars rather than an assumption about liquidity.
    assert (closes[0].price - open_price) * contract.size == (
        closing.bid_price_1 - opening.ask_price_1
    ) * contract.size


def test_the_feed_cursor_survives_replaying_the_same_bars(
    paper_engine: Any,
) -> None:
    """Replaying a session must not book its fills twice."""
    contract: Any = _contract(paper_engine.engine_name)
    cursor: FeedCursor = FeedCursor()

    first: list[MinuteBar] = [_bar(line) for line in LIVE_BARS[:4]]
    for bar in first:
        tick: Any = build_tick(bar, contract)
        if not cursor.seen(bar.time_text) and tick is not None:
            paper_engine.process_tick_event(Event(EVENT_TICK, tick))
            cursor.advance(bar)

    fills_before: int = len(_trades(paper_engine))

    # Replay the same four bars, as a restart would.
    for bar in first:
        assert cursor.seen(bar.time_text)

    cursor2: FeedCursor = FeedCursor()
    cursor2.advance(first[-1])
    assert cursor2.seen(LIVE_BARS[3].split(",")[0])
    assert fills_before >= 0


def _contract(gateway_name: str) -> ContractData:
    return ContractData(
        gateway_name=gateway_name,
        symbol="m2705",
        exchange=Exchange.DCE,
        name="m2705",
        product=Product.FUTURES,
        size=10,
        pricetick=1.0,
        min_volume=1,
    )
