"""The always-on recorder loop (R1): records the selection NOVA pushed, with the pushed session.

Every `poll_seconds` it reads `sync_state`. On a weekday between 08:59 and 15:31 IST (NOVA D90:
connect before the 09:00 pre-open and keep the 09:15–15:30 regular session inside the window),
with `enabled` on and a live session (`expires_at` in the future), it opens a `recordings` row and
records until 15:31 or `enabled` goes off. A push with a different live session or list ends the
recording at the next check; the next recording connects at once with the new values.
A failed recording is retried after 10 s, 30 s, then every 60 s (NOVA D79).
"""

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from websockets.asyncio.client import connect

from nova_rec.crypto import TokenCipher
from nova_rec.models import IndexTick, RecIndex, Recording, RecStock, SyncState, Tick
from nova_rec.recorder import Recorder, Sink, kite_url

logger = logging.getLogger("nova.rec.loop")

IST = ZoneInfo("Asia/Kolkata")
RECORD_START = time(8, 59)
RECORD_END = time(15, 31)
CHECK_SECONDS = 5.0
# Waits after consecutive failed recordings; a recording that ran RESET_AFTER starts again at 10 s.
RETRY_STEPS = (timedelta(seconds=10), timedelta(seconds=30), timedelta(seconds=60))
RESET_AFTER = timedelta(minutes=5)
ERROR_LENGTH = 500
# Kite streams at most 3,000 instruments per connection.
MAX_INSTRUMENTS = 3000
STOPPED = "Recorder stopped before the session ended; it resumes on restart"

# (url, instrument token → symbol, sink, should_stop): records until should_stop() is true.
Record = Callable[[str, dict[int, str], Sink, Callable[[], bool]], None]


def in_recording_window(now: datetime) -> bool:
    """Weekday 08:59–15:31 IST: connect before the 09:00 pre-open (NOVA D90)."""
    local = now.astimezone(IST)
    return local.weekday() < 5 and RECORD_START <= local.time() < RECORD_END


@dataclass(frozen=True)
class Target:
    """What one recording connects with: the pushed session (still sealed) and selection."""

    api_key: str
    sealed: bytes
    stocks: dict[int, str]
    indices: dict[int, str]


def load_target(db: Session, now: datetime) -> Target:
    """The pushed session and selection; ValueError says why there is nothing to record."""
    state = db.get(SyncState, 1)
    if state is None:
        raise ValueError("NOVA has not pushed a recording list yet")
    sealed, expires = state.access_token_encrypted, state.expires_at
    if sealed is None or expires is None or expires <= now:
        raise ValueError("No live Kite session: log in to Kite in NOVA")
    stocks = {
        int(token): symbol
        for symbol, token in db.execute(
            select(RecStock.symbol, RecStock.instrument_token).order_by(RecStock.symbol)
        )
    }
    if not stocks:
        raise ValueError("NOVA pushed no stock to record")
    indices = {
        int(token): name
        for name, token in db.execute(
            select(RecIndex.name, RecIndex.instrument_token).order_by(RecIndex.name)
        )
    }
    if len(stocks) + len(indices) > MAX_INSTRUMENTS:
        raise ValueError(f"Kite streams at most {MAX_INSTRUMENTS} instruments")
    return Target(state.api_key, sealed, stocks, indices)


def record_with_kite(
    url: str, tokens: dict[int, str], sink: Sink, should_stop: Callable[[], bool]
) -> None:
    recorder = Recorder(
        url=url, symbols=tokens, sink=sink, connect=connect, should_stop=should_stop
    )
    asyncio.run(recorder.run())


@dataclass
class RecorderLoop:
    """`cipher` is None without `NOVA_BROKER_TOKEN_KEY`: the loop then only idles."""

    factory: sessionmaker[Session]
    cipher: TokenCipher | None
    stop: threading.Event
    record: Record = record_with_kite
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    poll_seconds: float = 30.0
    retry_at: datetime | None = None
    failures: int = 0

    def run(self) -> None:
        self.recover()
        while not self.stop.is_set():
            self.step()
            self.stop.wait(self._wait_seconds())

    def _wait_seconds(self) -> float:
        """The poll interval, or less when a retry (or a restart after a push) is due sooner."""
        if self.retry_at is None:
            return self.poll_seconds
        left = (self.retry_at - self.now()).total_seconds()
        return min(self.poll_seconds, max(left, 0.0))

    def recover(self) -> None:
        """Fails recordings a stopped recorder left `running`; the next step starts a new one."""
        with self.factory() as db:
            for recording in db.scalars(select(Recording).where(Recording.status == "running")):
                self._end(recording, STOPPED)
            db.commit()

    def step(self) -> int | None:
        """Starts and runs one recording when it should; returns its id, or None."""
        now = self.now()
        if self.cipher is None:
            return None
        if self.retry_at is not None:
            if now < self.retry_at:
                return None
            self.retry_at = None
        with self.factory() as db:
            state = db.get(SyncState, 1)
            if state is None or not state.enabled or not in_recording_window(now):
                return None
            try:
                target = load_target(db, now)
                token = self.cipher.decrypt(target.sealed)
            except ValueError as exc:
                logger.info("Not recording: %s", exc)
                return None
            recording = Recording(started_at=now, status="running", rows_written=0)
            db.add(recording)
            db.commit()
            recording_id = recording.id
        logger.info(
            "Recording %s stock(s) and %s index(es) as #%s",
            len(target.stocks),
            len(target.indices),
            recording_id,
        )
        self._record(recording_id, target, kite_url(target.api_key, token))
        return recording_id

    def _record(self, recording_id: int, target: Target, url: str) -> None:
        started = last_check = self.now()
        ended: str | None = None

        # Any: rows are `ticks` / `index_ticks` column values for a bulk insert.
        def sink(rows: list[dict[str, Any]]) -> None:
            with self.factory() as db:
                if "exchange" not in rows[0]:
                    db.execute(insert(IndexTick).on_conflict_do_nothing(), rows)
                    db.commit()
                    return
                db.execute(insert(Tick).on_conflict_do_nothing(), rows)
                db.execute(
                    update(Recording)
                    .where(Recording.id == recording_id)
                    .values(rows_written=Recording.rows_written + len(rows))
                )
                db.commit()

        def should_stop() -> bool:
            nonlocal last_check, ended
            now = self.now()
            if self.stop.is_set() or not in_recording_window(now):
                return True
            if ended is None and (now - last_check).total_seconds() >= CHECK_SECONDS:
                last_check = now
                ended = self._ended(recording_id, target, now)
            return ended is not None

        error: str | None = None
        try:
            self.record(url, target.stocks | target.indices, sink, should_stop)
        except Exception as exc:  # any failure ends this recording only; the loop retries later
            logger.exception("Recording #%s failed", recording_id)
            error = str(exc) or type(exc).__name__
        if error is None and self.stop.is_set():
            error = STOPPED
        self._finish(recording_id, error, started, restart=ended == "changed")

    def _ended(self, recording_id: int, target: Target, now: datetime) -> str | None:
        """None while the recording goes on; else "off", "gone" or "changed" (a new push).

        Only `enabled: false` stops recording. A push without a live session or stocks keeps the
        working connection, and a database error never drops it (the next check tries again).
        """
        try:
            with self.factory() as db:
                recording = db.get(Recording, recording_id)
                if recording is None or recording.status != "running":
                    return "gone"
                state = db.get(SyncState, 1)
                if state is None or not state.enabled:
                    return "off"
                try:
                    current = load_target(db, now)
                except ValueError:
                    return None
                return None if current == target else "changed"
        except SQLAlchemyError:
            logger.warning("Checking the recording failed; still recording", exc_info=True)
            return None

    def _finish(
        self, recording_id: int, error: str | None, started: datetime, restart: bool
    ) -> None:
        with self.factory() as db:
            recording = db.get(Recording, recording_id)
            if recording is not None and recording.status == "running":
                self._end(recording, error)
            db.commit()
        if error is None or self.now() - started >= RESET_AFTER:
            self.failures = 0
        if error is not None:
            self.retry_at = self.now() + RETRY_STEPS[min(self.failures, len(RETRY_STEPS) - 1)]
            self.failures += 1
        elif restart:
            self.retry_at = self.now()  # a push changed the session or the list: connect again now
            logger.info("NOVA pushed a new session or list; reconnecting with it")

    def _end(self, recording: Recording, error: str | None) -> None:
        recording.status = "failed" if error else "completed"
        recording.error = error[:ERROR_LENGTH] if error else None
        recording.finished_at = self.now()
