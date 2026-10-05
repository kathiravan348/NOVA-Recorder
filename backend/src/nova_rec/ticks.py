"""Kite WebSocket binary ticks (D11, D49, D77).

A message is a big-endian int16 packet count, then per packet an int16 length and the packet.
Equity packets: `ltp` 8 bytes, `quote` 44 bytes (adds quantities and day OHLC), `full` 184 bytes
(adds timestamps, open interest and 5-level depth). Prices are paise. A 1-byte message is a
heartbeat. Index packets (28/32 bytes) carry prices and optional exchange time (D84).
"""

import struct
from dataclasses import dataclass, replace
from datetime import UTC, datetime

LTP, QUOTE, FULL = 8, 44, 184
INDEX_QUOTE, INDEX_FULL = 28, 32
DEPTH_START, DEPTH_LEVELS, DEPTH_ENTRY = 64, 5, 12


@dataclass(frozen=True)
class ParsedTick:
    token: int
    last_price_paise: int
    last_qty: int
    volume: int
    oi: int | None
    exchange_ts: datetime | None
    # Quote and full packets only (D77).
    avg_price_paise: int | None = None
    buy_qty: int | None = None
    sell_qty: int | None = None
    open_paise: int | None = None
    high_paise: int | None = None
    low_paise: int | None = None
    # Kite's `close`: the previous trading day's close during the session.
    close_paise: int | None = None
    # Full packets only (D77).
    last_trade_ts: datetime | None = None
    oi_day_high: int | None = None
    oi_day_low: int | None = None
    # Best 5 levels each side, best first: price (paise), quantity, number of orders.
    bid_price_paise: list[int] | None = None
    bid_qty: list[int] | None = None
    bid_orders: list[int] | None = None
    ask_price_paise: list[int] | None = None
    ask_qty: list[int] | None = None
    ask_orders: list[int] | None = None


@dataclass(frozen=True)
class ParsedIndexTick:
    token: int
    last_price_paise: int
    high_paise: int | None
    low_paise: int | None
    open_paise: int | None
    close_paise: int | None
    exchange_ts: datetime | None


def _ints(packet: bytes, count: int) -> tuple[int, ...]:
    return struct.unpack(f">{count}i", packet[: count * 4])


def _stamp(seconds: int) -> datetime | None:
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


def _depth(packet: bytes) -> list[tuple[int, int, int]]:
    """Ten (quantity, price, orders) entries: 5 bids, then 5 asks (2 padding bytes each)."""
    entries = []
    for level in range(2 * DEPTH_LEVELS):
        start = DEPTH_START + level * DEPTH_ENTRY
        entries.append(struct.unpack(">iih", packet[start : start + 10]))
    return entries


def _parse(packet: bytes) -> ParsedTick | ParsedIndexTick | None:
    if len(packet) in (INDEX_QUOTE, INDEX_FULL):
        token, ltp, high, low, open_, close, _change = _ints(packet, 7)
        stamp = _stamp(_ints(packet, 8)[7]) if len(packet) == INDEX_FULL else None
        # A non-positive day price means "not known yet"; `index_ticks` only accepts positive ones.
        high_, low_, open_p, close_p = (v if v > 0 else None for v in (high, low, open_, close))
        return ParsedIndexTick(token, ltp, high_, low_, open_p, close_p, stamp)
    if len(packet) == LTP:
        token, ltp = _ints(packet, 2)
        return ParsedTick(token, ltp, 0, 0, None, None)
    if len(packet) not in (QUOTE, FULL):
        return None
    token, ltp, qty, avg, volume, buy, sell, open_, high, low, close = _ints(packet, 11)
    quote = ParsedTick(token, ltp, qty, volume, None, None, avg, buy, sell, open_, high, low, close)
    if len(packet) == QUOTE:
        return quote
    values = _ints(packet, 16)
    depth = _depth(packet)
    bids, asks = depth[:DEPTH_LEVELS], depth[DEPTH_LEVELS:]
    return replace(
        quote,
        oi=values[12],
        exchange_ts=_stamp(values[15]),
        last_trade_ts=_stamp(values[11]),
        oi_day_high=values[13],
        oi_day_low=values[14],
        bid_price_paise=[e[1] for e in bids],
        bid_qty=[e[0] for e in bids],
        bid_orders=[e[2] for e in bids],
        ask_price_paise=[e[1] for e in asks],
        ask_qty=[e[0] for e in asks],
        ask_orders=[e[2] for e in asks],
    )


def parse_ticks(message: bytes) -> list[ParsedTick | ParsedIndexTick]:
    """Every stock or index tick in one binary message; heartbeats and broken frames give none."""
    if len(message) < 2:
        return []
    (count,) = struct.unpack(">h", message[:2])
    ticks, offset = [], 2
    for _ in range(count):
        if offset + 2 > len(message):
            break
        (length,) = struct.unpack(">h", message[offset : offset + 2])
        packet = message[offset + 2 : offset + 2 + length]
        offset += 2 + length
        if len(packet) != length:
            break
        tick = _parse(packet)
        if tick is not None and tick.last_price_paise > 0:
            ticks.append(tick)
    return ticks
