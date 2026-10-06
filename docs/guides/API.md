# Recorder — API reference (internal API for NOVA)

> State as of 6 Oct 2026 (REC-002). Service `rec-api`, host port `127.0.0.1:8100` (`REC_API_PORT`).
> NOVA's broker calls it at `http://host.docker.internal:8100`. Changing anything here needs a matching
> NOVA task (`AGENTS.md` §4).

**Common rules**
- Every `/internal/*` call needs header `x-rec-internal-token` = `REC_INTERNAL_TOKEN` (checked in constant
  time), else `401 {"detail": "Only NOVA may call this service"}`. The token is checked before the body.
- JSON field names are camelCase; unknown fields are refused (`422`). Times are ISO-8601 with a time zone.
- Errors look like `{"detail": "…"}`: `400` for a refused sync (message says why), `401`, `422` for a
  malformed body.
- No OpenAPI or docs pages are served.

| Method & path | What it does | Input | Output |
|---|---|---|---|
| `GET /health` | Liveness, no token | — | `{"status": "ok"}` |
| `PUT /internal/sync` | NOVA's push: replaces `sync_state`, `rec_stocks` and `rec_indices` in one transaction | `SyncIn` | `Status` |
| `GET /internal/status` | What Recorder is doing now | — | `Status` |
| `GET /internal/days` | Closed days, newest first; includes deleted days | — | `Day[]` |
| `GET /internal/days/{day}/stocks` | Stock counts for the closed IST day, sorted by symbol | — | `[{symbol, ticks}]` |
| `GET /internal/days/{day}/ticks` | Stream stock ticks using PostgreSQL binary COPY | — | `application/octet-stream`, `X-Row-Count` |
| `GET /internal/days/{day}/index-ticks` | Stream index ticks using PostgreSQL binary COPY | — | `application/octet-stream`, `X-Row-Count` |
| `POST /internal/days/{day}/imported` | Confirm matching counts; preserve the first import time | `{ticks, indexTicks}` | `200 Day` |
| `POST /internal/days/{day}/delete` | Request deletion; the recorder loop removes data outside the recording window | `{novaHasDay: bool}` | `202 Day` |

**Recorded days (REC-002; NOVA-224 / NOVA-227)**
`day` is an IST date (`YYYY-MM-DD`), derived from `received_at`, from midnight inclusive to the next
midnight exclusive. The loop closes weekdays with stock ticks outside the recording window, when no
recording is running; today becomes eligible at 15:31 IST. Counts freeze at closure.
```
Day = { day: date, ticks: int, indexTicks: int, stocks: int, closedAt: datetime,
        importedAt: datetime | null, deleteRequestedAt: datetime | null, deletedAt: datetime | null }
```
- An unknown or open day returns `404 {"detail": "Day not closed"}`. Invalid dates or bodies return 422.
- Binary exports use explicit columns in the same order as the tick tables (DATABASE.md), sorted by
  `received_at`. The stream includes the binary COPY header and trailer; it is ready for `COPY … FROM STDIN
  (FORMAT binary)`. `X-Row-Count` is the corresponding frozen count. Empty tables still return valid COPY.
  The database cursor sends chunks incrementally; an active export prevents deletion until it ends.
- Import counts must be nonnegative integers equal to the frozen counts, else
  `409 {"detail": {"ticks": <expected>, "indexTicks": <expected>}}`. Repeated confirmations keep the
  first `importedAt`. After a deletion request, confirmation returns 409 `Day deletion requested`.
- Deletion is manual only (R2): a closed day must have `importedAt`, or NOVA must send `novaHasDay: true`
  because it already holds that day from its own recorder. Otherwise return 409 `Day not imported`.
  The request only sets `deleteRequestedAt`; repeated requests keep its first value, including after
  deletion. There is no retention timer, and import alone never deletes data.
- After a deletion request, stocks and both exports return 410 `Day deletion requested`. The loop later
  removes the day's ticks, sets `deletedAt`, and keeps its registry row and frozen counts.

**`SyncIn`**
```
{ accountId: str, apiKey: str,
  accessTokenSealed: str | null,   // Kite access token sealed with NOVA_BROKER_TOKEN_KEY (Fernet), base64
  expiresAt: datetime | null,      // required when accessTokenSealed is sent
  enabled: bool,
  stocks:  [{ symbol: str, instrumentToken: int > 0 }],
  indices: [{ name: str,   instrumentToken: int > 0 }] }
```
`400` when: stocks + indices > 3,000 (Kite's limit per connection); a symbol, index name or instrument token
appears twice (tokens across stocks and indices together); `accessTokenSealed` is not base64; `expiresAt`
is missing with a token; or the token cannot be opened with Recorder's `NOVA_BROKER_TOKEN_KEY` (the keys
differ). A refused sync changes nothing. `accessTokenSealed: null` stores no session (`expiresAt` ignored).

**A sync during a recording:** a push with the same session and list changes nothing. A push with a
different live session (token or API key) or a different list ends the running recording within 5 s,
and the next recording connects at once with the new values (a gap of a few seconds). `enabled: false` ends
it without a new one. A push without a live session or without stocks keeps the running connection (the new
values apply from the next recording).

**`Status`**
```
{ state: "off" | "waiting" | "recording" | "no_login",
  syncedAt: datetime | null, sessionExpiresAt: datetime | null,
  stocks: int, indices: int,      // counts in the last push
  todayTicks: int,                // stock ticks saved by recordings that started today (IST)
  lastError: str | null }         // the latest recording's error; null if it ran or runs without one
```
- `off`: nothing pushed yet, or the last push had `enabled: false`.
- `recording`: a recording is running (a `recordings` row with status `running`).
- `no_login`: enabled, but no sealed token, or `expiresAt` has passed.
- `waiting`: enabled with a live session, not recording (outside weekdays 08:59–15:31 IST, or between retries).

**CLI** (inside the image): `python -m nova_rec migrate` (apply migrations, exit), `python -m nova_rec recorder`
(the always-on loop). The API runs as `uvicorn --factory nova_rec.api:app_factory`.
