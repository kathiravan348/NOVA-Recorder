"""Live tick recorder (D11, D49): Kite WebSocket → `ticks` in batches, reconnecting with backoff.

Interruptions (D79): a failed save keeps the ticks for the next batch instead of dropping the
connection, and a socket that sends no stock tick for `stall_limit(now)` seconds is reconnected:
10 s inside the 09:15–15:30 IST session, 60 s outside it (D81).
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from nova_rec.ticks import ParsedIndexTick, parse_ticks

logger = logging.getLogger("nova.rec.recorder")

KITE_WS = "wss://ws.kite.trade"
BATCH_SIZE = 500
BATCH_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
READ_TIMEOUT_SECONDS = 2.0
SESSION_STALL_SECONDS = 10.0
IDLE_STALL_SECONDS = 60.0
# Own copies: `recorder_loop` imports this module, so it cannot be imported here.
IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
MAX_BUFFER = 50_000


def stall_limit(now: datetime) -> float:
    """Seconds without a stock tick before a reconnect: 10 in the session, else 60 (D81)."""
    local = now.astimezone(IST)
    in_session = local.weekday() < 5 and SESSION_OPEN <= local.time() < SESSION_CLOSE
    return SESSION_STALL_SECONDS if in_session else IDLE_STALL_SECONDS


class Stalled(Exception):
    """The socket is open but no stock tick arrived for `stall_limit(now)` seconds."""


class Socket(Protocol):
    async def send(self, message: str) -> None: ...

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


Connect = Callable[[str], AbstractAsyncContextManager[Socket]]
# Any: a tick row is a dict of `ticks` column values for a bulk insert.
Sink = Callable[[list[dict[str, Any]]], None]


@dataclass
class Recorder:
    """`symbols`: instrument token → symbol. `now`, `sleep`, `connect` are injectable for tests."""

    url: str
    symbols: dict[int, str]
    sink: Sink
    connect: Connect
    should_stop: Callable[[], bool]
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    exchange: str = "NSE"
    read_timeout: float = READ_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self._last: dict[str, datetime] = {}
        self._buffer: list[dict[str, Any]] = []
        self._index_buffer: list[dict[str, Any]] = []
        self._flushed = self.now()
        self._failing = False
        self._last_tick = self.now()
        self._received = 0

    def _stamp(self, symbol: str) -> datetime:
        """Receive time, made unique per symbol (it is part of the primary key)."""
        stamp = self.now()
        last = self._last.get(symbol)
        if last is not None and stamp <= last:
            stamp = last + timedelta(microseconds=1)
        self._last[symbol] = stamp
        return stamp

    def _flush(self) -> bool:
        """Saves the buffer; on failure keeps it (newest `MAX_BUFFER` rows) and returns False."""
        self._flushed = self.now()
        saved = True
        for buffer in (self._buffer, self._index_buffer):
            if buffer and not self._save(buffer):
                saved = False
        self._failing = not saved
        return saved

    def _save(self, buffer: list[dict[str, Any]]) -> bool:
        """Save either sink independently: a failed index save never repeats saved stocks."""
        try:
            self.sink(list(buffer))
        except Exception:  # database trouble must not drop the socket: retry at the next batch
            logger.warning(
                "Saving %s tick(s) failed; keeping them for the next batch",
                len(buffer),
                exc_info=True,
            )
            dropped = len(buffer) - MAX_BUFFER
            if dropped > 0:
                del buffer[:dropped]
                logger.warning(
                    "Dropped the %s oldest unsaved tick(s) (over %s)", dropped, MAX_BUFFER
                )
            return False
        buffer.clear()
        return True

    def _flush_if_due(self) -> None:
        # While saves fail, retry once per batch interval, not on every 500 rows.
        due = (self.now() - self._flushed).total_seconds() >= BATCH_SECONDS
        if due or (len(self._buffer) + len(self._index_buffer) >= BATCH_SIZE and not self._failing):
            self._flush()

    def _take(self, message: bytes) -> None:
        for tick in parse_ticks(message):
            symbol = self.symbols.get(tick.token)
            if symbol is None:
                continue
            # Every tick field becomes a column (D77); each row has all keys for the bulk insert.
            row = asdict(tick)
            del row["token"]
            row |= {"symbol": symbol, "received_at": self._stamp(symbol)}
            if isinstance(tick, ParsedIndexTick):
                self._index_buffer.append(row)
            else:
                row["exchange"] = self.exchange
                self._buffer.append(row)
                self._last_tick = self.now()
                self._received += 1
        self._flush_if_due()

    async def _session(self) -> None:
        """One connection, until `should_stop()` or the socket ends; raises to reconnect."""
        tokens = list(self.symbols)
        async with self.connect(self.url) as socket:
            await socket.send(json.dumps({"a": "subscribe", "v": tokens}))
            await socket.send(json.dumps({"a": "mode", "v": ["full", tokens]}))
            logger.info("Recording %s symbol(s)", len(tokens))
            self._last_tick = self.now()
            messages = aiter(socket)
            # One pending read kept across timeouts: cancelling it would close the iterator.
            pending = asyncio.ensure_future(anext(messages))
            try:
                while True:
                    done, _ = await asyncio.wait({pending}, timeout=self.read_timeout)
                    if done:
                        try:
                            message = pending.result()
                        except StopAsyncIteration:
                            return
                        pending = asyncio.ensure_future(anext(messages))
                        if isinstance(message, bytes):
                            self._take(message)
                    else:
                        self._flush_if_due()
                    if self.should_stop():
                        return
                    now = self.now()
                    limit = stall_limit(now)
                    if (now - self._last_tick).total_seconds() >= limit:
                        raise Stalled(f"no ticks for {limit:.0f} s")
            finally:
                pending.cancel()

    async def run(self) -> None:
        """Records until `should_stop()`; any connection problem reconnects after 1, 2, 4 … 30 s.

        The wait starts again at 1 s once a connection has received ticks.
        """
        backoff = 1.0
        while not self.should_stop():
            received = self._received
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network, protocol, server errors or a stall: reconnect
                if self._received > received:
                    backoff = 1.0
                logger.warning("Tick socket dropped (%s); reconnecting in %.0f s", exc, backoff)
                self._flush()
                await self.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        if not self._flush():
            logger.error(
                "Lost %s unsaved tick(s) at the end of the recording",
                len(self._buffer) + len(self._index_buffer),
            )


def kite_url(api_key: str, access_token: str) -> str:
    return f"{KITE_WS}?api_key={api_key}&access_token={access_token}"
