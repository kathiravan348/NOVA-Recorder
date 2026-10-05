"""The tick recorder with a fake socket and clock (D49)."""

import asyncio
import json
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from nova_rec import recorder as recorder_module
from nova_rec.models import Tick
from nova_rec.recorder import BATCH_SIZE, Recorder, Socket, stall_limit

START = datetime(2026, 9, 25, 4, 0, tzinfo=UTC)


def ltp_frame(token: int, price: int) -> bytes:
    return struct.pack(">hh2i", 1, 8, token, price)


def full_frame(token: int, price: int, close: int) -> bytes:
    """One full packet; depth level n (bids 0–4, asks 5–9) has n orders."""
    head = struct.pack(">16i", token, price, 1, price, 10, 0, 0, 0, 0, 0, close, 0, 0, 0, 0, 0)
    depth = b"".join(struct.pack(">iihxx", 1, price, n) for n in range(10))
    return struct.pack(">hh", 1, 184) + head + depth


class Clock:
    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> datetime:
        return self.at


class FakeSocket:
    def __init__(self, messages: list[str | bytes], clock: Clock, step: timedelta) -> None:
        self.messages, self.clock, self.step = messages, clock, step
        self.sent: list[object] = []
        self.exhausted = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def __aiter__(self) -> AsyncIterator[str | bytes]:
        for message in self.messages:
            self.clock.at += self.step
            yield message
        self.exhausted = True


class Harness:
    """Hands out one scripted session per connect; an `OSError` entry makes that connect fail."""

    def __init__(self, sessions: list[list[str | bytes] | OSError], step: timedelta) -> None:
        self.clock, self.sessions, self.step = Clock(), sessions, step
        self.batches: list[list[dict[str, Any]]] = []
        self.sleeps: list[float] = []
        self.sockets: list[FakeSocket] = []

    @asynccontextmanager
    async def connect(self, url: str) -> AsyncIterator[Socket]:
        session = self.sessions.pop(0)
        if isinstance(session, OSError):
            raise session
        socket = FakeSocket(session, self.clock, self.step)
        self.sockets.append(socket)
        yield socket

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def finished(self) -> bool:
        return not self.sessions and bool(self.sockets) and self.sockets[-1].exhausted

    def recorder(self, symbols: dict[int, str]) -> Recorder:
        return Recorder(
            url="wss://fake",
            symbols=symbols,
            sink=self.batches.append,
            connect=self.connect,
            should_stop=self.finished,
            now=self.clock,
            sleep=self.sleep,
        )


def run(harness: Harness, symbols: dict[int, str]) -> None:
    asyncio.run(harness.recorder(symbols).run())


def test_subscribes_in_full_mode_and_batches_by_time() -> None:
    messages: list[str | bytes] = [ltp_frame(1, 100)] * 5
    harness = Harness([[*messages, b"\x00", "text"]], timedelta(milliseconds=300))
    run(harness, {1: "INFY"})
    assert harness.sockets[0].sent == [
        {"a": "subscribe", "v": [1]},
        {"a": "mode", "v": ["full", [1]]},
    ]
    assert [len(b) for b in harness.batches] == [4, 1]
    row = harness.batches[0][0]
    assert (row["exchange"], row["symbol"], row["last_price_paise"]) == ("NSE", "INFY", 100)


def test_rows_carry_every_tick_column() -> None:
    """LTP and full ticks give rows with the same keys: every `ticks` column (D77)."""
    harness = Harness([[ltp_frame(7, 100), full_frame(7, 101, 99)]], timedelta(0))
    run(harness, {7: "INFY"})
    ltp_row, full_row = harness.batches[0]
    assert set(ltp_row) == set(full_row) == set(Tick.__table__.columns.keys())
    assert ltp_row["bid_qty"] is None and ltp_row["close_paise"] is None
    assert full_row["close_paise"] == 99
    assert full_row["ask_orders"] == [5, 6, 7, 8, 9]


def test_batches_by_size_and_unique_receive_times() -> None:
    harness = Harness([[ltp_frame(1, 100)] * (BATCH_SIZE + 3)], timedelta(0))
    run(harness, {1: "INFY"})
    assert [len(b) for b in harness.batches] == [BATCH_SIZE, 3]
    stamps = [row["received_at"] for batch in harness.batches for row in batch]
    assert len(set(stamps)) == len(stamps)


def test_unknown_tokens_are_dropped() -> None:
    harness = Harness([[ltp_frame(1, 100), ltp_frame(2, 200)]], timedelta(seconds=2))
    run(harness, {2: "TCS"})
    assert [row["symbol"] for batch in harness.batches for row in batch] == ["TCS"]


def test_reconnects_with_doubling_backoff_capped_at_30_seconds() -> None:
    sessions: list[list[str | bytes] | OSError] = [OSError("down")] * 7 + [[ltp_frame(1, 5)]]
    harness = Harness(sessions, timedelta(seconds=2))
    run(harness, {1: "INFY"})
    assert harness.sleeps == [1, 2, 4, 8, 16, 30, 30]
    assert len(harness.sockets) == 1
    assert [len(b) for b in harness.batches] == [1]


def test_stops_mid_session() -> None:
    harness = Harness([[ltp_frame(1, 5)] * 3], timedelta(milliseconds=1))
    recorder = harness.recorder({1: "INFY"})
    recorder.should_stop = lambda: harness.clock.at > START
    asyncio.run(recorder.run())
    assert sum(len(b) for b in harness.batches) == 1


def test_a_failed_save_keeps_the_ticks_and_the_connection() -> None:
    """D79: two failed saves, then one save with every tick, on the same socket."""
    harness = Harness([[ltp_frame(1, 100 + n) for n in range(5)]], timedelta(seconds=1))
    failures = 2

    def sink(rows: list[dict[str, Any]]) -> None:
        nonlocal failures
        if failures:
            failures -= 1
            raise OSError("database down")
        harness.batches.append(list(rows))

    recorder = harness.recorder({1: "INFY"})
    recorder.sink = sink
    asyncio.run(recorder.run())
    saved = [row["last_price_paise"] for batch in harness.batches for row in batch]
    assert saved == [100, 101, 102, 103, 104]
    assert len(harness.sockets) == 1 and harness.sleeps == []


def test_unsaved_ticks_over_the_cap_drop_the_oldest(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_BUFFER", 3)
    harness = Harness([[ltp_frame(1, 100 + n) for n in range(5)]], timedelta(seconds=1))

    def sink(rows: list[dict[str, Any]]) -> None:
        raise OSError("database down")

    recorder = harness.recorder({1: "INFY"})
    recorder.sink = sink
    asyncio.run(recorder.run())
    assert [row["last_price_paise"] for row in recorder._buffer] == [102, 103, 104]
    assert "Dropped the 1 oldest unsaved tick(s)" in caplog.text
    assert "Lost 3 unsaved tick(s)" in caplog.text


def test_in_the_session_10_seconds_of_heartbeats_reconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness([[b"\x00"] * 20, [ltp_frame(1, 5)]], timedelta(seconds=1))  # 09:30 IST
    run(harness, {1: "INFY"})
    assert harness.sleeps == [1]
    assert len(harness.sockets) == 2 and not harness.sockets[0].exhausted
    assert [len(b) for b in harness.batches] == [1]
    assert "no ticks for 10 s" in caplog.text


def test_before_the_open_30_seconds_of_heartbeats_do_not_reconnect() -> None:
    harness = Harness([[b"\x00"] * 30, [ltp_frame(1, 5)]], timedelta(seconds=1))
    harness.clock.at = datetime(2026, 9, 25, 3, 44, 0, tzinfo=UTC)  # 09:14:00 IST
    run(harness, {1: "INFY"})  # the first socket ends at 09:14:30, inside the 60 s limit
    assert harness.sleeps == []
    assert harness.sockets[0].exhausted


def test_outside_the_session_60_seconds_of_heartbeats_reconnect() -> None:
    harness = Harness([[b"\x00"] * 61, [ltp_frame(1, 5)]], timedelta(seconds=1))
    harness.clock.at = datetime(2026, 9, 26, 4, 0, tzinfo=UTC)  # Saturday 09:30 IST
    run(harness, {1: "INFY"})
    assert len(harness.sockets) == 2 and not harness.sockets[0].exhausted


@pytest.mark.parametrize(
    ("ist", "limit"),
    [
        (datetime(2026, 9, 25, 9, 14, 59), 60.0),
        (datetime(2026, 9, 25, 9, 15, 0), 10.0),
        (datetime(2026, 9, 25, 15, 29, 59), 10.0),
        (datetime(2026, 9, 25, 15, 30, 0), 60.0),
        (datetime(2026, 9, 26, 11, 0, 0), 60.0),  # Saturday
    ],
)
def test_stall_limit_is_10_s_in_the_session_else_60(ist: datetime, limit: float) -> None:
    assert stall_limit(ist.replace(tzinfo=recorder_module.IST)) == limit


def test_backoff_starts_again_after_a_connection_that_got_ticks() -> None:
    stalled: list[str | bytes] = [ltp_frame(1, 5), *[b"\x00"] * 11]
    harness = Harness(
        [OSError("down"), OSError("down"), stalled, [ltp_frame(1, 6)]], timedelta(seconds=1)
    )
    run(harness, {1: "INFY"})
    assert harness.sleeps == [1, 2, 1]


class SilentSocket:
    async def send(self, message: str) -> None:
        pass

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self

    async def __anext__(self) -> str | bytes:
        await asyncio.Event().wait()
        raise StopAsyncIteration


def test_a_silent_socket_still_checks_should_stop() -> None:
    checks = 0

    def should_stop() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    @asynccontextmanager
    async def connect(url: str) -> AsyncIterator[Socket]:
        yield SilentSocket()

    recorder = Recorder(
        url="wss://fake",
        symbols={1: "INFY"},
        sink=lambda rows: None,
        connect=connect,
        should_stop=should_stop,
        read_timeout=0.01,
    )
    asyncio.run(asyncio.wait_for(recorder.run(), timeout=2))
    assert checks == 3  # loop start, after one read timeout, loop end


def index_frame(token: int = 9) -> bytes:
    return struct.pack(
        ">hh7i", 1, 28, token, 2_500_000, 2_510_000, 2_490_000, 2_495_000, 2_480_000, 200
    )


def test_mixed_message_uses_separate_stock_and_index_batches() -> None:
    from nova_rec.models import IndexTick

    message = struct.pack(">h", 2) + ltp_frame(1, 100)[2:] + index_frame()[2:]
    harness = Harness([[message]], timedelta(0))
    run(harness, {1: "INFY", 9: "NIFTY 50"})
    stocks, indices = harness.batches
    assert set(stocks[0]) == set(Tick.__table__.columns.keys())
    assert set(indices[0]) == set(IndexTick.__table__.columns.keys())
    assert indices[0]["symbol"] == "NIFTY 50"


def test_index_ticks_do_not_reset_the_stock_watchdog() -> None:
    harness = Harness([[index_frame()] * 20, [ltp_frame(1, 100)]], timedelta(seconds=1))
    run(harness, {1: "INFY", 9: "NIFTY 50"})
    assert harness.sleeps == [1]
    assert len(harness.sockets) == 2 and not harness.sockets[0].exhausted


def test_failed_index_save_keeps_only_the_unsaved_index_rows() -> None:
    harness = Harness([[ltp_frame(1, 100), index_frame(), index_frame()]], timedelta(seconds=1))
    recorder = harness.recorder({1: "INFY", 9: "NIFTY 50"})
    failures = 1

    def sink(rows: list[dict[str, Any]]) -> None:
        nonlocal failures
        if "exchange" not in rows[0] and failures:
            failures -= 1
            raise OSError("index save failed")
        harness.batches.append(rows)

    recorder.sink = sink
    asyncio.run(recorder.run())
    saved = [row for batch in harness.batches for row in batch]
    assert sum(row["symbol"] == "INFY" for row in saved) == 1
    assert sum(row["symbol"] == "NIFTY 50" for row in saved) == 2
