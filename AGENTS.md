# Recorder — Agent Rulebook

> Every agent reads this file first, every session, then its own file: `CLAUDE.md`, `CHATGPT.md` or `GEMINI.md`.
> Keep it short: details live in `docs/`. Recorder is the live tick recorder of NOVA, a private trading platform
> for one family (`C:\Users\kathi\Dev\Projects\NOVA`). Why it is a separate repo: `docs/DECISIONS.md` R1 (NOVA D91).

## 1. Scope and primary rule
**Scope:** record Kite live ticks for the selection NOVA pushes; keep each day until the Owner deletes it from
NOVA Relay (R2: manual delete only). No UI, backtests, orders or Kite calls other than the WebSocket. NOVA is the only client.
Kite has no tick history (NOVA D11): a tick not recorded is lost for good.

**Primary rule — recording never stops.** On weekdays 08:55–15:45 IST never stop, restart, rebuild or recreate
any `nova-rec` container (no `docker compose up`, `down`, `stop`, `restart` or `build` of its services).
Deploy after 15:45 IST or at weekends. Tests use `rec-test-db` only, never `rec-postgres`;
`docker compose run --rm rec-check` is allowed at any time (it starts only `rec-test-db`).

## 2. Roles
Three roles: **planner** (task files, decisions), **implementer** (builds a task), **reviewer** (reviews, fixes, merges).

| Agent | Roles it may take | Own file |
|---|---|---|
| Claude (Claude Code) | Planner, implementer, reviewer | `CLAUDE.md` |
| ChatGPT (ChatGPT app / Codex) | Planner, implementer, reviewer | `CHATGPT.md` |
| Gemini (Antigravity) | Implementer **only** | `GEMINI.md` |
| Owner (human) | Approves plans, merges final decisions | — |

**Lead agents** = Claude and ChatGPT. Either may stand in for the other, or for Gemini. Gemini never stands in for a lead agent.
- The Owner says the role ("Plan", "Review", "Build REC-###"). No role given: a lead agent works as planner/reviewer
  (review first, then plan); Gemini works as implementer.
- One role per task at a time. May change code: the implementer only for tasks `in-progress` / `changes-requested` it
  owns; the reviewer only for tasks `in-review` it owns; the planner only task files, the board and `docs/DECISIONS.md`.
- Only a lead agent edits `AGENTS.md`, the agent files, `docs/DECISIONS.md` and task files' scope, and only as planner or reviewer.
- An implementer never changes a plan. If a task is unclear or wrong, stop and write the question in its `Questions` section.
- **No self-review by default:** Claude built → ChatGPT reviews, and the other way round. If the Owner says the other lead
  agent is not available, the same agent may review in a **fresh session** and writes `Self-review: yes` in the review note.
- **Taking over:** set yourself as owner on the board, read the task's `Questions`, `Handoff` and branch commits, and
  continue on the same branch. Never take over a task another agent is still working on unless the Owner says it has stopped.

## 3. Task workflow (status = lock)
Board: `docs/tasks/BOARD.md`. Read it at the start of every session. `Owner` column = the agent doing the current step.

`draft` (planner writing) → `planned` → `in-progress` (implementer) → `ready-for-review` → `in-review` (reviewer) → `done`

From `in-review`, the reviewer may send a task back: `changes-requested` (implementer) → `ready-for-review`.

1. Change the status on the board **before** starting work, and set yourself as owner.
2. Never edit files of a task you do not own. Two tasks whose `Files` overlap may not be in progress or in review at once
   (`docs/guides/*.md` and `docs/DECISIONS.md` do not count: edit only your own lines, keep both sides on a conflict).
3. One branch per task: `task/REC-###`. Commit all work (`REC-###: …`) on it before `ready-for-review`. Reviewer fixes
   go on the same branch with prefix `review:`.
4. Lead agent priority: review `ready-for-review` tasks first, then plan.
5. The reviewer fixes small/medium issues directly; if the fix means rewriting most of the task, it writes a precise
   change request and sets `changes-requested`.
6. Only a lead agent acting as reviewer merges to `main`, after `rec-check` passes. Gemini never merges.
7. Decisions are `R#` in `docs/DECISIONS.md`. A change spanning NOVA and Recorder is one task on each board, linked
   by *Depends on* (NOVA-### ↔ REC-###). Tasks may be planned from NOVA's board (NOVA D91).

## 4. Interface with NOVA
NOVA talks to Recorder only through the internal API in `docs/guides/API.md`. Changing it needs a matching task on
NOVA's board (NOVA D91 (1)). Recorder shares no code, database, network or Docker objects with NOVA; it never calls NOVA.
`ticks` and `index_ticks` keep NOVA's exact columns so a day copies across as-is; changing them needs NOVA tasks too.

## 5. Secrets
- The Kite access token arrives **sealed**; it is opened only in memory with `NOVA_BROKER_TOKEN_KEY` (same value as
  NOVA's `.env`). `NOVA_BROKER_TOKEN_KEY` and `REC_INTERNAL_TOKEN` live only in `.env` (git-ignored).
- Never log, print, return or commit a token or key, sealed or not. Never put secrets in code, fixtures or tests.

## 6. Token-saving rules (mandatory)
- Read only: this file, `docs/tasks/BOARD.md`, your task file, and the files the task lists. Nothing else unless the task says so.
- One task per session. Handoff note ≤ 20 lines (`docs/templates/HANDOFF.md`); review note ≤ 20 lines (`docs/templates/REVIEW.md`).
- The reviewer reads `git diff main...task/REC-###`, not whole files. Prove work with tests, not by re-reading code.
- Do not restate the task or this rulebook in replies. Report only: done / changed files / open questions.

## 7. Tech stack and machine (do not add others without a task saying so)
Python 3.12 (Docker; host only via uv), uv, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2 + Alembic, psycopg,
PostgreSQL + TimescaleDB, websockets, cryptography, Docker Compose; ruff, mypy `--strict`, pytest.
Pin exact versions (`==`) in `backend/pyproject.toml`, locked in `backend/uv.lock`, matching NOVA's pins where shared.
New dependency = note it in the handoff and ask in `docs/DECISIONS.md` *Pending*.
- **Owner PC:** Windows 11; WSL2/Docker capped at 12 GB RAM + 12 CPUs, shared with NOVA. uv cache on `E:\caches\uv`.
  PowerShell 5.1 is the default shell: no `&&` there.
- **Python: uv only.** Never `pip`, `python -m venv` or `uv pip install --system`. Add deps with `uv add` / `uv add --dev`
  inside `backend/`. Fast host checks while working: `uv run --directory backend pytest`.
- **Docker** (names fixed by NOVA D91 (2)): Compose project `nova-rec`; containers `rec-postgres` (TimescaleDB,
  `127.0.0.1:5433`, volume `rec-db-data`), `rec-migrate`, `rec-recorder`, `rec-api` (`127.0.0.1:8100`); image
  `nova-rec:dev`; `rec-test-db` and `rec-check` in profile `tools`. Every port binds `127.0.0.1`; every service has a
  `mem_limit` (whole stack ≤ 1 GB). Never prune images/volumes or change Docker/WSL settings (Owner only).

## 8. Code rules
- Full type hints (mypy strict), no `Any` without a comment saying why. Named, small modules; files ≤ 300 lines.
- Pydantic models forbid extra fields; camelCase on the wire. Prices as integer paise (NOVA's columns), never `float`.
- Times timezone-aware UTC in storage; a trading day is an IST (`Asia/Kolkata`) date.
- Every endpoint has a test. Tests never call Kite or NOVA (fake WebSocket, fake client).
- Infrastructure settings only from environment (`pydantic-settings`). Line endings LF (`.gitattributes`).

## 9. Living guides
`docs/guides/` explains what is built today: `SETUP.md` (first start, `.env`, commands), `API.md` (every endpoint),
`DATABASE.md` (every table, migration number in the header). Update them **in the same task** as the change.
Describe only what is merged and works; copy names exactly; change the "State as of" line (date + last task);
replace outdated text, do not append history. Nothing changed: write `Guides: none` in the handoff.

## 10. Definition of done (every task)
- [ ] Acceptance checks in the task file all pass
- [ ] `docker compose run --rm rec-check` passes (ruff, format, mypy strict, pytest)
- [ ] Guides updated (`SETUP`, `API`, `DATABASE`) or handoff says `Guides: none`
- [ ] Handoff note written in the task file; board status updated

## 11. Never
- Never stop, restart, rebuild or recreate a `nova-rec` container on weekdays 08:55–15:45 IST (§1).
- Never `docker compose down -v` or delete `rec-db-data`: it holds days NOVA has not imported yet.
- Never delete a day without a delete request from NOVA; refuse one for a day neither imported nor marked `novaHasDay` (R2).
- Never place, modify or cancel orders, and never call Kite except the tick WebSocket.
- Never touch NOVA's repo, containers, database or volumes from a Recorder task.
- Never put keys, secrets or tokens in code, logs, fixtures or commits (§5).
- Never delete tests to make a check pass.
