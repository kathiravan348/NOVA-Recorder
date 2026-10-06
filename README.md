# Recorder

Live Kite tick recorder for NOVA, a private trading platform for one family. It records the stocks and indices
NOVA pushes each morning, keeps every day until the Owner deletes it from NOVA Relay, and talks only to NOVA.

- Rules for agents: `AGENTS.md`, then `CLAUDE.md`, `CHATGPT.md` or `GEMINI.md`.
- Tasks: `docs/tasks/BOARD.md`. Decisions: `docs/DECISIONS.md` (R1 = NOVA D91).
- Setup: `docs/guides/SETUP.md`. Internal API: `docs/guides/API.md`. Tables: `docs/guides/DATABASE.md`.

State as of 5 Oct 2026 (REC-001): the stack records once NOVA pushes a list and a Kite session (NOVA-223).
First start and commands: `docs/guides/SETUP.md`.
