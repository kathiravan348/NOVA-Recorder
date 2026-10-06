# Recorder — Task board

> Read at the start of every session. Update status + owner BEFORE starting work.
> Statuses: draft · planned · in-progress · ready-for-review · in-review · changes-requested · done
> A task is written when it has a file in `docs/tasks/`; until then its status is `draft`.
> A NOVA-### task in *Depends on* is on NOVA's board (`NOVA\docs\tasks\BOARD.md`).

| ID | Title | Status | Owner | Depends on |
|---|---|---|---|---|
| REC-001 | Recorder runs on its own: stack, database, sync API, recording loop | done | Claude | NOVA-222 |
| REC-002 | Recorded days: list, export, mark imported, delete on request (R2) | in-review | Claude | REC-001 |

## Order and notes
- REC-001 → REC-002, then NOVA-223 → 224 → 225 → 226 on NOVA's board (NOVA D91).
- Build and deploy only outside weekdays 08:55–15:45 IST (`AGENTS.md` §1).
