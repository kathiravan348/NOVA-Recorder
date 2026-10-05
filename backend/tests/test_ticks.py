"""Kite binary tick parsing (D49)."""

import struct
from datetime import UTC, datetime

from nova_rec.ticks import ParsedTick, parse_ticks


def frame(*packets: bytes) -> bytes:
    body = b"".join(struct.pack(">h", len(p)) + p for p in packets)
    return struct.pack(">h", len(packets)) + body


def ltp(token: int, price: int) -> bytes:
    return struct.pack(">2i", token, price)


def quote(token: int, price: int, qty: int, volume: int) -> bytes:
    return struct.pack(">11i", token, price, qty, price, volume, 0, 0, 0, 0, 0, 0)


def full(token: int, price: int, qty: int, volume: int, oi: int, stamp: int) -> bytes:
    values = (token, price, qty, price, volume, 0, 0, 0, 0, 0, 0, stamp, oi, 0, 0, stamp)
    head = struct.pack(">16i", *values)
    return head + bytes(184 - len(head))


def test_ltp_quote_and_full_packets() -> None:
    message = frame(
        ltp(1, 150_025), quote(2, 99_900, 10, 5_000), full(3, 20_000, 5, 900, 77, 1_790_000_000)
    )
    ticks = [tick for tick in parse_ticks(message) if isinstance(tick, ParsedTick)]
    assert len(ticks) == 3
    assert [(t.token, t.last_price_paise, t.last_qty, t.volume, t.oi) for t in ticks] == [
        (1, 150_025, 0, 0, None),
        (2, 99_900, 10, 5_000, None),
        (3, 20_000, 5, 900, 77),
    ]
    assert ticks[0].exchange_ts is None
    assert ticks[2].exchange_ts == datetime.fromtimestamp(1_790_000_000, UTC)


def full_packet() -> bytes:
    """A full packet with every field set to a distinct value (D77)."""
    head = struct.pack(
        ">16i",
        7,  # token
        20_050,  # last price
        15,  # last quantity
        20_010,  # average price
        123_456,  # volume
        4_000,  # total buy quantity
        5_000,  # total sell quantity
        19_900,  # open
        20_200,  # high
        19_800,  # low
        19_950,  # close (previous day)
        1_790_000_100,  # last trade time
        0,  # open interest
        11,  # OI day high
        3,  # OI day low
        1_790_000_105,  # exchange time
    )
    bids = [struct.pack(">iihxx", 100 + i, 20_045 - 5 * i, 1 + i) for i in range(5)]
    asks = [struct.pack(">iihxx", 200 + i, 20_055 + 5 * i, 10 + i) for i in range(5)]
    return head + b"".join(bids + asks)


def test_full_packet_keeps_every_field() -> None:
    (tick,) = parse_ticks(frame(full_packet()))
    assert isinstance(tick, ParsedTick)
    assert (tick.token, tick.last_price_paise, tick.last_qty) == (7, 20_050, 15)
    assert tick.volume == 123_456
    assert (tick.avg_price_paise, tick.buy_qty, tick.sell_qty) == (20_010, 4_000, 5_000)
    assert (tick.open_paise, tick.high_paise, tick.low_paise) == (19_900, 20_200, 19_800)
    assert tick.close_paise == 19_950
    assert (tick.oi, tick.oi_day_high, tick.oi_day_low) == (0, 11, 3)
    assert tick.last_trade_ts == datetime.fromtimestamp(1_790_000_100, UTC)
    assert tick.exchange_ts == datetime.fromtimestamp(1_790_000_105, UTC)
    assert tick.bid_price_paise == [20_045, 20_040, 20_035, 20_030, 20_025]
    assert tick.bid_qty == [100, 101, 102, 103, 104]
    assert tick.bid_orders == [1, 2, 3, 4, 5]
    assert tick.ask_price_paise == [20_055, 20_060, 20_065, 20_070, 20_075]
    assert tick.ask_qty == [200, 201, 202, 203, 204]
    assert tick.ask_orders == [10, 11, 12, 13, 14]


def test_quote_packet_keeps_quantities_and_ohlc_but_no_depth() -> None:
    values = (2, 99_900, 10, 99_800, 5_000, 70, 80, 99_000, 99_950, 98_000, 99_100)
    packet = struct.pack(">11i", *values)
    (tick,) = parse_ticks(frame(packet))
    assert isinstance(tick, ParsedTick)
    assert (tick.avg_price_paise, tick.buy_qty, tick.sell_qty) == (99_800, 70, 80)
    assert (tick.open_paise, tick.high_paise, tick.low_paise, tick.close_paise) == (
        99_000,
        99_950,
        98_000,
        99_100,
    )
    assert tick.last_trade_ts is None and tick.bid_price_paise is None


def test_heartbeat_and_empty_give_nothing() -> None:
    assert parse_ticks(b"\x00") == []
    assert parse_ticks(b"") == []


def test_unknown_lengths_and_zero_prices_are_skipped() -> None:
    index = bytes(12)
    assert [t.token for t in parse_ticks(frame(index, ltp(4, 0), ltp(5, 10)))] == [5]


def test_truncated_frame_keeps_the_complete_packets() -> None:
    message = frame(ltp(1, 100), quote(2, 200, 1, 1))[:-5]
    assert [t.token for t in parse_ticks(message)] == [1]


def test_garbage_does_not_raise() -> None:
    assert parse_ticks(b"\x7f\xff\x00\x08abc") == []


def test_index_quote_and_full_keep_prices_and_exchange_time() -> None:
    from nova_rec.ticks import ParsedIndexTick

    values = (256265, 2_500_000, 2_510_000, 2_490_000, 2_495_000, 2_480_000, 200)
    quote_tick, full_tick = parse_ticks(
        frame(struct.pack(">7i", *values), struct.pack(">8i", *values, 1_790_000_000))
    )
    assert isinstance(quote_tick, ParsedIndexTick) and isinstance(full_tick, ParsedIndexTick)
    assert (
        quote_tick.last_price_paise,
        quote_tick.high_paise,
        quote_tick.low_paise,
        quote_tick.open_paise,
        quote_tick.close_paise,
    ) == values[1:6]
    assert quote_tick.exchange_ts is None
    assert full_tick.exchange_ts == datetime.fromtimestamp(1_790_000_000, UTC)


def test_index_zero_price_is_skipped_and_unknown_day_prices_are_none() -> None:
    from nova_rec.ticks import ParsedIndexTick

    skipped = struct.pack(">7i", 1, 0, 10, 10, 10, 10, 0)
    early = struct.pack(">7i", 2, 2_500_000, 0, -1, 0, 2_480_000, 0)
    (tick,) = parse_ticks(frame(skipped, early))
    assert isinstance(tick, ParsedIndexTick) and tick.token == 2
    assert (tick.high_paise, tick.low_paise, tick.open_paise) == (None, None, None)
    assert tick.close_paise == 2_480_000
