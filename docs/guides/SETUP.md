# Recorder — Setup (first start, `.env`, commands)

> State as of 5 Oct 2026 (REC-001). Run every command in `C:\Users\kathi\Dev\Projects\NOVA-Recorder`.
> Weekdays 08:55–15:45 IST: never start, stop, restart, rebuild or recreate `nova-rec` services
> (`AGENTS.md` §1). Only `docker compose run --rm rec-check` is allowed then.

## First start (after 15:45 IST or at a weekend)
1. Copy `.env.example` to `.env`. Set `REC_DB_PASSWORD`, a long random `REC_INTERNAL_TOKEN`, and
   `NOVA_BROKER_TOKEN_KEY` = the same value as in NOVA's `.env` (without it the recorder only waits).
2. `docker compose up -d --build` — builds `nova-rec:dev`, starts `rec-postgres`, runs `rec-migrate`, then
   starts `rec-recorder` and `rec-api`. NOVA's containers are not touched (separate project `nova-rec`).
3. Check: `curl http://127.0.0.1:8100/health` → `{"status":"ok"}`.
4. Recording starts only after NOVA pushes a list and a live Kite session (`PUT /internal/sync`,
   `docs/guides/API.md`), then on weekdays 08:59–15:31 IST.

## `.env`

| Variable | Meaning |
|---|---|
| `REC_DB_USER`, `REC_DB_PASSWORD`, `REC_DB_NAME` | Recorder's own database on `rec-postgres` |
| `REC_DB_PORT` | Host port for `rec-postgres` (default 5433, bound to 127.0.0.1) |
| `REC_API_PORT` | Host port for `rec-api` (default 8100, bound to 127.0.0.1) |
| `REC_INTERNAL_TOKEN` | Token NOVA must send in `x-rec-internal-token` (NOVA's `NOVA_REC_TOKEN`) |
| `NOVA_BROKER_TOKEN_KEY` | Same as NOVA's: opens the sealed Kite token in memory only |
| `REC_LOG_LEVEL` | `debug` / `info` (default) / `warning` / `error` |

## Commands

| Command | What it does |
|---|---|
| `docker compose run --rm rec-check` | All checks: ruff, format, mypy strict, pytest. Starts `rec-test-db` (in-memory test server, 256 MB) and uses the mounted code, so no rebuild. Allowed at any time once `nova-rec:dev` exists. `rec-test-db` keeps running afterwards; stop it with `docker compose stop rec-test-db` after 15:45. |
| `docker compose up -d --build` | Deploy a change (after 15:45 IST or at weekends only). |
| `docker compose logs -f rec-recorder` | Follow the recorder (`nova.rec.*` lines; tokens are never logged). |
| `curl http://127.0.0.1:8100/health` | Is the API up? |
| `uv run --directory backend pytest` | Quick host run; database tests are skipped without `REC_TEST_DATABASE_URL`. |

From NOVA's broker container: `docker exec nova-broker-kite python -c "import urllib.request as u;
print(u.urlopen('http://host.docker.internal:8100/health').status)"` → `200`.

Never `docker compose down -v` and never delete `rec-db-data`: it holds days NOVA has not imported yet.
