# Recorder — Decisions log

> Newest last. One short entry per decision. Only a lead agent (Claude or ChatGPT, as planner or reviewer) or the
> Owner adds entries. Decisions that span both repos live in NOVA's log (`NOVA\docs\DECISIONS.md`).

| # | Date | Decision | Why |
|---|---|---|---|
| R1 | 2026-10-05 | Recorder is a standalone repo and Docker stack — summary of NOVA **D91** (full text there). (1) Own repo, rulebook, board (`REC-###`), decisions (`R#`) and guides; shares no code or Docker objects with NOVA; a change spanning both repos is one task on each board, linked by *Depends on*. (2) Compose project `nova-rec`: `rec-postgres` (TimescaleDB, `127.0.0.1:5433`, volume `rec-db-data`), `rec-migrate`, `rec-recorder`, `rec-api` (`127.0.0.1:8100`), image `nova-rec:dev`; tests use `rec-test-db` only. (3) Code copied once from NOVA's recorder, then owned here; `ticks`/`index_ticks` keep NOVA's exact columns; no Redis; no Kite calls except the WebSocket. (4) Morning push: NOVA sends `PUT /internal/sync` (header `x-rec-internal-token`) with the session still sealed (same `NOVA_BROKER_TOKEN_KEY` in both `.env` files), `enabled`, stocks and indices; after it Recorder needs nothing from NOVA all day. (5) Evening import: NOVA lists finished days, streams each day (binary COPY), checks the counts and confirms the import; Recorder deletes a day only 7 days after a confirmed import. (6) Switch-over (NOVA-226): both recorders side by side for ≥ 2 trading days, then NOVA's recorder is removed and this repo's primary rule (never touch `nova-rec` containers on weekdays 08:55–15:45 IST) replaces NOVA D76. | NOVA work was blocked all day by live recording (shared Postgres, one image, no restarts). Kite has no tick history, so recording is the one process that must be isolated. |

## Pending
- Private GitHub repo for Recorder — Owner creates it and gives the URL; push only after that (NOVA-222).
