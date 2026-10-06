"""Frozen IST days, bounded binary COPY exports and requested deletion (R2)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from psycopg import Connection, sql
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from nova_rec.models import IndexTick, RecordedDay, Recording, Tick

IST = ZoneInfo("Asia/Kolkata")
RECORD_START = time(8, 59)
RECORD_END = time(15, 31)
TICK_COLUMNS = (
    "exchange",
    "symbol",
    "received_at",
    "exchange_ts",
    "last_price_paise",
    "last_qty",
    "volume",
    "oi",
    "avg_price_paise",
    "buy_qty",
    "sell_qty",
    "open_paise",
    "high_paise",
    "low_paise",
    "close_paise",
    "last_trade_ts",
    "oi_day_high",
    "oi_day_low",
    "bid_price_paise",
    "bid_qty",
    "bid_orders",
    "ask_price_paise",
    "ask_qty",
    "ask_orders",
)
INDEX_TICK_COLUMNS = (
    "symbol",
    "received_at",
    "exchange_ts",
    "last_price_paise",
    "high_paise",
    "low_paise",
    "open_paise",
    "close_paise",
)


def in_recording_window(now: datetime) -> bool:
    local = now.astimezone(IST)
    return local.weekday() < 5 and RECORD_START <= local.time() < RECORD_END


def bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), IST).astimezone(UTC)
    return start, start + timedelta(days=1)


def get_day(db: Session, day: date, *, lock: bool = False) -> RecordedDay:
    query = select(RecordedDay).where(RecordedDay.day == day)
    if lock:
        query = query.with_for_update()
    row = db.scalar(query)
    if row is None:
        raise HTTPException(404, "Day not closed")
    return row


def close_days(db: Session, now: datetime) -> None:
    """Only finish weekdays with data, after their recording window has ended."""
    local = now.astimezone(IST)
    cutoff = local.date() if local.time() >= RECORD_END else local.date() - timedelta(days=1)
    # Days close in order, so only ticks after the last closed day are scanned (this runs every
    # loop step outside the window; a full-table scan would grow with every kept day).
    last = db.scalar(select(func.max(RecordedDay.day)))
    upper = bounds(cutoff)[1]
    lower = bounds(last + timedelta(days=1))[0] if last is not None else None
    candidates: set[date] = set()
    for table in (Tick, IndexTick):
        day_expr = func.date(func.timezone("Asia/Kolkata", table.received_at))
        query = select(day_expr).where(table.received_at < upper).distinct()
        if lower is not None:
            query = query.where(table.received_at >= lower)
        candidates.update(db.scalars(query))
    for day in sorted(candidates):
        if day.weekday() >= 5 or db.get(RecordedDay, day) is not None:
            continue
        start, end = bounds(day)
        ticks, stocks = db.execute(
            select(func.count(), func.count(func.distinct(Tick.symbol))).where(
                Tick.received_at >= start, Tick.received_at < end
            )
        ).one()
        indices = db.scalar(
            select(func.count()).where(IndexTick.received_at >= start, IndexTick.received_at < end)
        )
        db.add(
            RecordedDay(
                day=day, ticks=ticks, index_ticks=indices or 0, stocks=stocks, closed_at=now
            )
        )


def _delete_table_day(db: Session, table: type[Tick] | type[IndexTick], day: date) -> None:
    """Drop only chunks fully contained in this IST day; delete boundary rows separately."""
    start, end = bounds(day)
    db.execute(
        text(
            "SELECT drop_chunks(CAST(:table AS regclass), older_than => CAST(:end AS timestamptz), "
            "newer_than => CAST(:start AS timestamptz))"
        ),
        {"table": table.__tablename__, "start": start, "end": end},
    )
    db.execute(delete(table).where(table.received_at >= start, table.received_at < end))


def maintain_days(db: Session, now: datetime) -> None:
    """No maintenance while recording; imported days without a request stay forever."""
    if in_recording_window(now) or db.scalar(
        select(func.count()).where(Recording.status == "running")
    ):
        return
    # Serialize maintenance even if a second loop is accidentally started.
    if not db.scalar(text("SELECT pg_try_advisory_xact_lock(2002)")):
        return
    close_days(db, now)
    db.flush()
    for row in db.scalars(
        select(RecordedDay)
        .where(RecordedDay.delete_requested_at.is_not(None), RecordedDay.deleted_at.is_(None))
        .order_by(RecordedDay.day)
        .with_for_update()
    ):
        _delete_table_day(db, Tick, row.day)
        _delete_table_day(db, IndexTick, row.day)
        row.deleted_at = now


def confirm_import(db: Session, day: date, ticks: int, indices: int, now: datetime) -> RecordedDay:
    row = get_day(db, day, lock=True)
    if row.delete_requested_at is not None:
        raise HTTPException(409, "Day deletion requested")
    if (ticks, indices) != (row.ticks, row.index_ticks):
        raise HTTPException(409, {"ticks": row.ticks, "indexTicks": row.index_ticks})
    if row.imported_at is None:
        row.imported_at = now
    db.commit()
    return row


def request_delete(db: Session, day: date, nova_has_day: bool, now: datetime) -> RecordedDay:
    row = get_day(db, day, lock=True)
    if row.delete_requested_at is None:
        if row.imported_at is None and not nova_has_day:
            raise HTTPException(409, "Day not imported")
        row.delete_requested_at = now
    db.commit()
    return row


def stock_counts(db: Session, day: date) -> list[dict[str, str | int]]:
    row = db.scalar(select(RecordedDay).where(RecordedDay.day == day).with_for_update(read=True))
    if row is None:
        raise HTTPException(404, "Day not closed")
    if row.delete_requested_at is not None:
        raise HTTPException(410, "Day deletion requested")
    start, end = bounds(day)
    return [
        {"symbol": symbol, "ticks": count}
        for symbol, count in db.execute(
            select(Tick.symbol, func.count())
            .where(Tick.received_at >= start, Tick.received_at < end)
            .group_by(Tick.symbol)
            .order_by(Tick.symbol)
        )
    ]


class ExportResponse(StreamingResponse):
    """Release the export lock even when a client disconnects before consuming the iterator."""

    def __init__(self, chunks: Iterator[bytes], db: Session, count: int) -> None:
        self.db = db
        super().__init__(
            chunks, media_type="application/octet-stream", headers={"X-Row-Count": str(count)}
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.db.close()


def export_day(factory: sessionmaker[Session], day: date, *, indices: bool) -> StreamingResponse:
    """Hold a shared row lock until COPY ends, so maintenance cannot remove an active export."""
    db = factory()
    try:
        row = db.scalar(
            select(RecordedDay).where(RecordedDay.day == day).with_for_update(read=True)
        )
        if row is None:
            raise HTTPException(404, "Day not closed")
        if row.delete_requested_at is not None:
            raise HTTPException(410, "Day deletion requested")
        count = row.index_ticks if indices else row.ticks
        table = "index_ticks" if indices else "ticks"
        columns = INDEX_TICK_COLUMNS if indices else TICK_COLUMNS
        statement = sql.SQL(
            "COPY (SELECT {} FROM {} WHERE received_at >= %s AND received_at < %s "
            "ORDER BY received_at) TO STDOUT (FORMAT binary)"
        ).format(sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier(table))
        connection = cast(
            Connection[tuple[object, ...]], db.connection().connection.driver_connection
        )

        def chunks() -> Iterator[bytes]:
            try:
                with connection.cursor() as cursor, cursor.copy(statement, bounds(day)) as copy:
                    # libpq yields COPY blocks incrementally, with no day-sized buffer.
                    for block in copy:
                        yield bytes(block)
            finally:
                db.close()

        return ExportResponse(chunks(), db, count)
    except BaseException:
        db.close()
        raise
