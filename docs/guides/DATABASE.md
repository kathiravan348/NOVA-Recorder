# Recorder — Database (tables; migration 0001)

> State as of 5 Oct 2026 (REC-001). Server `rec-postgres` (TimescaleDB 2.30.1, PostgreSQL 17),
> `127.0.0.1:5433`, volume `rec-db-data`. Migrations: `backend/src/nova_rec/migrations/versions/`
> (`python -m nova_rec migrate`, run by `rec-migrate`). Tests use `rec-test-db` only.

## Recorded ticks (same columns as NOVA, so a day copies across as-is)

| Table | Primary key | Notes |
|---|---|---|
| `ticks` | `exchange`, `symbol`, `received_at` | One stock tick. Hypertable on `received_at`, 1-day chunks, no compression. |
| `index_ticks` | `symbol`, `received_at` | One index tick (`symbol` = index name, no foreign key). Hypertable on `received_at`, 1-day chunks. |

`ticks` columns: `exchange` text (`NSE`/`NFO`), `symbol` text, `received_at` timestamptz, `exchange_ts`
timestamptz null, `last_price_paise` bigint, `last_qty` bigint, `volume` bigint, `oi` bigint null,
`avg_price_paise`, `buy_qty`, `sell_qty`, `open_paise`, `high_paise`, `low_paise`, `close_paise` bigint null,
`last_trade_ts` timestamptz null, `oi_day_high`, `oi_day_low` bigint null, `bid_price_paise`, `bid_qty`,
`ask_price_paise`, `ask_qty` bigint[] null, `bid_orders`, `ask_orders` integer[] null (best 5 levels, best first).
Checks: `ck_ticks_exchange`, `ck_ticks_values` (`last_price_paise > 0 AND last_qty >= 0 AND volume >= 0`).

`index_ticks` columns: `symbol` text, `received_at` timestamptz, `exchange_ts` timestamptz null,
`last_price_paise` bigint, `high_paise`, `low_paise`, `open_paise`, `close_paise` bigint null (each `> 0`).

`received_at` is the receive time, made unique per symbol (1 µs steps). Rows are written in batches
(1 s or 500 rows); a duplicate key is skipped.

## What NOVA pushed

| Table | Columns | Notes |
|---|---|---|
| `sync_state` | `id` smallint PK (always 1), `account_id` text, `api_key` text, `access_token_encrypted` bytea null, `expires_at` timestamptz null, `enabled` bool, `synced_at` timestamptz | The last `PUT /internal/sync`. The token stays sealed (Fernet, `NOVA_BROKER_TOKEN_KEY`). |
| `rec_stocks` | `symbol` text PK, `instrument_token` bigint unique | Stocks to record (NSE). |
| `rec_indices` | `name` text PK, `instrument_token` bigint | Indices to record. |

A sync replaces all three in one transaction.

## Recordings

| Table | Columns | Notes |
|---|---|---|
| `recordings` | `id` bigint identity PK, `started_at` timestamptz, `finished_at` timestamptz null, `status` text (`running`/`completed`/`failed`), `error` text null (≤ 500 chars), `rows_written` bigint default 0 | One run of the recorder loop; `rows_written` counts stock ticks only. A row left `running` by a stopped recorder is set `failed` on restart. |
