"""The always-on loop with a fake clock and a fake Kite socket; the migration matches the models."""

import asyncio
import json
import struct
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import ACCESS_TOKEN, API_KEY, FRIDAY_10_IST, Clock
from sqlalchemy import Engine, delete, func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from nova_rec import db as rec_db
from nova_rec.crypto import TokenCipher
from nova_rec.loop import RecorderLoop, in_recording_window
from nova_rec.models import IndexTick, RecIndex, Recording, RecStock, SyncState, Tick
from nova_rec.recorder import Recorder, Sink, Socket

SATURDAY_10_IST = FRIDAY_10_IST + timedelta(days=1)
AT_1531 = FRIDAY_10_IST.replace(hour=10, minute=1)


def ist(hour: int, minute: int, second: int = 0) -> datetime:
    """Friday 25 Sep 2026 at this IST time."""
    return FRIDAY_10_IST.replace(hour=0, minute=0) + timedelta(
        hours=hour - 5, minutes=minute - 30, seconds=second
    )


@pytest.mark.parametrize(
    ("at", "inside"),
    [
        (ist(8, 58, 59), False),
        (ist(8, 59), True),
        (ist(9, 0), True),
        (ist(15, 30, 59), True),
        (ist(15, 31), False),
        (SATURDAY_10_IST, False),
        (SATURDAY_10_IST + timedelta(days=1), False),  # Sunday
    ],
)
def test_recording_window_is_weekdays_0859_to_1531_ist(at: datetime, inside: bool) -> None:
    assert in_recording_window(at) is inside


def rows(count: int, at: datetime) -> list[dict[str, Any]]:
    """Any: `ticks` column values."""
    return [
        {
            "exchange": "NSE",
            "symbol": "INFY",
            "received_at": at + timedelta(microseconds=i),
            "exchange_ts": None,
            "last_price_paise": 150000,
            "last_qty": 1,
            "volume": 10,
            "oi": None,
        }
        for i in range(count)
    ]


Script = Callable[[Sink, Callable[[], bool], Clock], None]


def push(engine: Engine, key: str, enabled: bool = True, token: str = ACCESS_TOKEN) -> None:
    """What NOVA's `PUT /internal/sync` leaves: INFY, TCS and NIFTY 50, a live session."""
    with Session(engine) as db:
        db.merge(
            SyncState(
                id=1,
                account_id="acc_owner",
                api_key=API_KEY,
                access_token_encrypted=TokenCipher(key).encrypt(token),
                expires_at=FRIDAY_10_IST + timedelta(days=1),
                enabled=enabled,
                synced_at=FRIDAY_10_IST,
            )
        )
        if db.scalar(select(func.count()).select_from(RecStock)) == 0:
            db.add_all(
                [
                    RecStock(symbol="INFY", instrument_token=408065),
                    RecStock(symbol="TCS", instrument_token=2953217),
                    RecIndex(name="NIFTY 50", instrument_token=256265),
                ]
            )
        db.commit()


def make_loop(engine: Engine, key: str, clock: Clock, script: Script) -> RecorderLoop:
    def record(
        url: str, tokens: dict[int, str], sink: Sink, should_stop: Callable[[], bool]
    ) -> None:
        assert url.endswith(f"api_key={API_KEY}&access_token={ACCESS_TOKEN}")
        assert sorted(tokens.values()) == ["INFY", "NIFTY 50", "TCS"]
        script(sink, should_stop, clock)

    return RecorderLoop(
        factory=sessionmaker(bind=engine, expire_on_commit=False),
        cipher=TokenCipher(key),
        stop=threading.Event(),
        record=record,
        now=clock,
    )


def recording(engine: Engine, recording_id: int) -> Recording:
    with Session(engine) as db:
        found = db.get(Recording, recording_id)
        assert found is not None
        return found


def until_close(sink: Sink, should_stop: Callable[[], bool], clock: Clock) -> None:
    sink(rows(3, clock.at))
    clock.at = FRIDAY_10_IST.replace(hour=10, minute=0)  # 15:30 IST: still recording
    assert not should_stop()
    clock.at = AT_1531
    assert should_stop()


def test_migration_matches_the_models(engine: Engine, database_url: str) -> None:
    assert rec_db.diff(database_url) == []


def test_ticks_and_index_ticks_are_daily_hypertables(engine: Engine) -> None:
    with Session(engine) as db:
        found = db.execute(
            text(
                "SELECT hypertable_name, column_name, time_interval"
                " FROM timescaledb_information.dimensions ORDER BY hypertable_name"
            )
        ).all()
    assert [(t, c, i) for t, c, i in found] == [
        ("index_ticks", "received_at", timedelta(days=1)),
        ("ticks", "received_at", timedelta(days=1)),
    ]


def test_a_recording_starts_at_0859_ist(engine: Engine, key: str) -> None:
    push(engine, key)
    assert make_loop(engine, key, Clock(ist(8, 59)), until_close).step() is not None


@pytest.mark.parametrize("at", [ist(8, 58, 59), AT_1531, SATURDAY_10_IST])
def test_no_recording_before_0859_from_1531_or_at_weekends(
    engine: Engine, key: str, at: datetime
) -> None:
    push(engine, key)
    assert make_loop(engine, key, Clock(at), until_close).step() is None


def test_switched_off_does_nothing(engine: Engine, key: str) -> None:
    push(engine, key, enabled=False)
    assert make_loop(engine, key, Clock(FRIDAY_10_IST), until_close).step() is None


def test_nothing_pushed_or_no_live_session_does_nothing(engine: Engine, key: str) -> None:
    loop = make_loop(engine, key, Clock(FRIDAY_10_IST), until_close)
    assert loop.step() is None
    push(engine, key)
    with Session(engine) as db:
        db.execute(update(SyncState).values(access_token_encrypted=None, expires_at=None))
        db.commit()
    assert loop.step() is None
    push(engine, key)
    loop.now = Clock(FRIDAY_10_IST + timedelta(days=1))  # expired at this instant
    assert loop.step() is None
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Recording)) == 0


def test_without_the_token_key_the_loop_only_waits(engine: Engine, key: str) -> None:
    push(engine, key)
    loop = make_loop(engine, key, Clock(FRIDAY_10_IST), until_close)
    loop.cipher = None
    assert loop.step() is None


def test_records_a_session_as_a_completed_recording(engine: Engine, key: str) -> None:
    push(engine, key)

    recording_id = make_loop(engine, key, Clock(FRIDAY_10_IST), until_close).step()

    assert recording_id is not None
    done = recording(engine, recording_id)
    assert done.status == "completed" and done.rows_written == 3
    assert done.finished_at == AT_1531 and done.error is None
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Tick)) == 3


def test_switching_off_ends_the_recording(engine: Engine, key: str) -> None:
    push(engine, key)

    def script(sink: Sink, should_stop: Callable[[], bool], clock: Clock) -> None:
        sink(rows(2, clock.at))
        clock.at += timedelta(seconds=6)
        assert not should_stop()
        push(engine, key, enabled=False)
        clock.at += timedelta(seconds=6)
        assert should_stop()

    loop = make_loop(engine, key, Clock(FRIDAY_10_IST), script)
    recording_id = loop.step()

    assert recording_id is not None
    ended = recording(engine, recording_id)
    assert ended.status == "completed" and ended.rows_written == 2
    assert loop.retry_at is None


def test_a_push_with_the_same_values_does_not_interrupt(engine: Engine, key: str) -> None:
    push(engine, key)

    def script(sink: Sink, should_stop: Callable[[], bool], clock: Clock) -> None:
        push(engine, key)
        clock.at += timedelta(seconds=6)
        assert not should_stop()
        clock.at = AT_1531
        assert should_stop()

    assert make_loop(engine, key, Clock(FRIDAY_10_IST), script).step() is not None


def test_a_new_session_or_list_reconnects_at_once_with_it(engine: Engine, key: str) -> None:
    push(engine, key)
    clock = Clock(FRIDAY_10_IST)

    def script(sink: Sink, should_stop: Callable[[], bool], clock: Clock) -> None:
        with Session(engine) as db:
            db.execute(update(RecIndex).values(instrument_token=260105))
            db.commit()
        clock.at += timedelta(seconds=6)
        assert should_stop()

    loop = make_loop(engine, key, clock, script)
    first = loop.step()

    assert first is not None and recording(engine, first).status == "completed"
    assert loop.retry_at == clock.at and loop._wait_seconds() == 0
    loop.record = lambda url, tokens, sink, should_stop: None
    assert loop.step() is not None  # no wait for the next poll


def test_a_push_without_a_session_keeps_the_running_connection(engine: Engine, key: str) -> None:
    push(engine, key)

    def script(sink: Sink, should_stop: Callable[[], bool], clock: Clock) -> None:
        with Session(engine) as db:
            db.execute(update(SyncState).values(access_token_encrypted=None, expires_at=None))
            db.execute(delete(RecStock))
            db.commit()
        clock.at += timedelta(seconds=6)
        assert not should_stop()
        clock.at = AT_1531
        assert should_stop()

    loop = make_loop(engine, key, Clock(FRIDAY_10_IST), script)
    recording_id = loop.step()

    assert recording_id is not None and recording(engine, recording_id).status == "completed"
    assert loop.retry_at is None


def test_a_database_error_in_the_check_keeps_recording(engine: Engine, key: str) -> None:
    push(engine, key)
    working = sessionmaker(bind=engine, expire_on_commit=False)

    def broken() -> Session:
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))

    def script(sink: Sink, should_stop: Callable[[], bool], clock: Clock) -> None:
        loop.factory = broken  # type: ignore[assignment]  # every session fails
        clock.at += timedelta(seconds=6)
        assert not should_stop()
        loop.factory = working
        clock.at = AT_1531
        assert should_stop()

    loop = make_loop(engine, key, Clock(FRIDAY_10_IST), script)
    recording_id = loop.step()

    assert recording_id is not None and recording(engine, recording_id).status == "completed"


def test_a_failure_fails_the_recording_and_retries_after_10_30_60_seconds(
    engine: Engine, key: str
) -> None:
    push(engine, key)

    def script(sink: Sink, should_stop: Callable[[], bool], clock: Clock) -> None:
        raise RuntimeError("socket refused")

    clock = Clock(FRIDAY_10_IST)
    loop = make_loop(engine, key, clock, script)
    recording_id = loop.step()

    assert recording_id is not None
    failed = recording(engine, recording_id)
    assert failed.status == "failed" and failed.error == "socket refused"
    for wait in (10, 30, 60, 60):
        clock.at += timedelta(seconds=wait - 1)
        assert loop.step() is None
        clock.at += timedelta(seconds=1)
        assert loop.step() is not None


def test_a_recording_that_ran_5_minutes_retries_after_10_seconds(engine: Engine, key: str) -> None:
    push(engine, key)

    def script(sink: Sink, should_stop: Callable[[], bool], clock: Clock) -> None:
        clock.at += timedelta(minutes=5)
        raise RuntimeError("socket refused")

    clock = Clock(FRIDAY_10_IST)
    loop = make_loop(engine, key, clock, script)
    for _ in range(3):
        assert loop.step() is not None
        clock.at += timedelta(seconds=10)


def test_a_restart_fails_the_recording_it_left_running(engine: Engine, key: str) -> None:
    with Session(engine) as db:
        db.add(Recording(id=41, started_at=FRIDAY_10_IST, status="running"))
        db.commit()

    make_loop(engine, key, Clock(FRIDAY_10_IST), until_close).recover()

    left = recording(engine, 41)
    assert left.status == "failed" and left.error is not None


def ltp_frame(token: int, price: int) -> bytes:
    return struct.pack(">hh2i", 1, 8, token, price)


def index_frame(token: int) -> bytes:
    return struct.pack(
        ">hh7i", 1, 28, token, 2_500_000, 2_510_000, 2_490_000, 2_495_000, 2_480_000, 200
    )


class FakeKite:
    """A fake Kite WebSocket: sends the frames, one second apart, then the session closes."""

    def __init__(self, frames: list[bytes], clock: Clock) -> None:
        self.frames, self.clock = frames, clock
        self.sent: list[object] = []
        self.urls: list[str] = []

    @asynccontextmanager
    async def connect(self, url: str) -> AsyncIterator[Socket]:
        self.urls.append(url)
        yield self

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def __aiter__(self) -> AsyncIterator[str | bytes]:
        for frame in self.frames:
            self.clock.at += timedelta(seconds=1)
            yield frame
        self.clock.at = AT_1531


def test_a_fake_websocket_recording_writes_ticks_index_ticks_and_a_recording(
    engine: Engine, key: str
) -> None:
    push(engine, key)
    clock = Clock(FRIDAY_10_IST)
    kite = FakeKite(
        [ltp_frame(408065, 150_000), index_frame(256265), ltp_frame(2953217, 400_000)], clock
    )

    def record(
        url: str, tokens: dict[int, str], sink: Sink, should_stop: Callable[[], bool]
    ) -> None:
        recorder = Recorder(
            url=url,
            symbols=tokens,
            sink=sink,
            connect=kite.connect,
            should_stop=should_stop,
            now=clock,
        )
        asyncio.run(recorder.run())

    loop = RecorderLoop(
        factory=sessionmaker(bind=engine, expire_on_commit=False),
        cipher=TokenCipher(key),
        stop=threading.Event(),
        record=record,
        now=clock,
    )
    recording_id = loop.step()

    assert recording_id is not None
    assert kite.urls == [f"wss://ws.kite.trade?api_key={API_KEY}&access_token={ACCESS_TOKEN}"]
    assert kite.sent[1] == {"a": "mode", "v": ["full", [408065, 2953217, 256265]]}
    done = recording(engine, recording_id)
    assert done.status == "completed" and done.rows_written == 2
    with Session(engine) as db:
        assert sorted(db.scalars(select(Tick.symbol))) == ["INFY", "TCS"]
        index = db.scalars(select(IndexTick)).one()
    assert index.symbol == "NIFTY 50" and index.last_price_paise == 2_500_000
    assert index.received_at.tzinfo is not None and index.received_at > FRIDAY_10_IST
