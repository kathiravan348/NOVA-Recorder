# Recorder — API reference (internal API for NOVA)

> State as of 5 Oct 2026 (REC-001). Service `rec-api`, host port `127.0.0.1:8100` (`REC_API_PORT`).
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

**A sync during a recording:** a push with the same session and list changes nothing. A push that changes
the session (token, API key, or a session that is no longer live) or the list ends the running recording
within 5 s, and the next recording connects at once with the new values (a gap of a few seconds).
`enabled: false` ends it without a new one.

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
