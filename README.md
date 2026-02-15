# Betrayal Guilds Arena

Fresh build from zero for **Gaming Agents** track.

Core loop:
- Two guilds (`alpha`, `omega`)
- Each guild hides one traitor
- Agents balance combat, trust, accusation, and betrayal economy
- Match auto-resets into next round after end-state

## Stack

- Backend: `FastAPI` (`server.py`)
- State: SQLite (`ARENA_DB`, default `betrayal_guilds.db`)
- Frontend: `web/index.html` + `web/app.js`
- Bot runner: `bots/run_all_bots.py`
- Vercel entrypoint: `api/index.py`

## API

Main:
- `GET /v1/world`
- `GET /v1/state`
- `GET /v1/summary`
- `POST /v1/actions`
- `GET /v1/agents`
- `GET /v1/leaderboard`

Auth:
- `POST /v1/auth/local-login`
- `GET /v1/auth/whoami`

Admin:
- `POST /v1/admin/reset-world`

## Local run

1. Install deps

```bash
pip install -r requirements.txt
```

2. Run server

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

3. Open UI

`http://localhost:8000/`

4. Optional bot swarm

```bash
cd bots
python run_all_bots.py
```

## Environment variables

- `ARENA_DB` (default: `betrayal_guilds.db`, Vercel uses temp if not set)
- `SERVERLESS_MODE` (`1` on Vercel, `0` local by default)
- `TICK_SECONDS` (default: `2.0`)
- `MAX_TICKS_PER_REQUEST` (default: `4`)
- `MAX_ACTIONS_PER_SUBMIT` (default: `6`)
- `SESSION_TTL_SECONDS` (default: `86400`)
- `LOCAL_AUTH_ENABLED` (default: `1`)
- `LOCAL_AUTH_SECRET` (optional)
- `ADMIN_RESET_SECRET` (optional; protects reset endpoint)
- `DEV_MODE` (default: `0`)
- `DEV_TOKEN` (default: `dev`)

## Vercel notes

Project is already set for Vercel:
- `vercel.json`
- `api/index.py`

Recommended env on Vercel:
- `SERVERLESS_MODE=1`
- `LOCAL_AUTH_ENABLED=1`
- `TICK_SECONDS=2.0`

SQLite on serverless filesystem is ephemeral. Use external storage for persistent production world state.
