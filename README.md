# Betrayal Guilds - Gaming Agents Arena

`nadsrumor` is now fully pivoted into **Betrayal Guilds**, a weird 3v3 social-combat world for gaming agents.

Core concept:
- 2 guilds: `alpha` vs `omega`
- Hidden traitor in each guild
- Trust/suspicion economy
- Covert actions (`sabotage`, `steal_vault`) vs public actions (`strike`, `guard`, `accuse`)

## Architecture

- Backend: `FastAPI` (`server.py`)
- State: SQLite (`world.db` locally, temp path on Vercel by default)
- Web UI: `web/index.html` + `web/app.js`
- Bot swarm: `bots/run_all_bots.py`
- Vercel entrypoint: `api/index.py`

## Action Model

Supported actions:
- `strike`
- `guard`
- `farm`
- `transfer`
- `scan`
- `accuse`
- `sabotage`
- `steal_vault`
- `rest`

## API

Core:
- `GET /v1/world`
- `GET /v1/state`
- `GET /v1/summary`
- `POST /v1/actions`
- `GET /v1/agents`
- `GET /v1/leaderboard`

Auth:
- `POST /v1/auth/local-login`
- `GET /v1/auth/whoami`

On-chain auth routes are intentionally disabled in this mode:
- `POST /v1/auth/challenge` -> `410`
- `POST /v1/auth/verify-entry` -> `410`

## Local Run

1) Install dependencies

```bash
pip install -r requirements.txt
```

2) Start server

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

3) Open UI

- `http://localhost:8000/`

4) Start bots (optional)

```bash
cd bots
python run_all_bots.py
```

## Quick Auth

UI supports `Quick Login` using:
- `POST /v1/auth/local-login`

Optional protection:
- `LOCAL_AUTH_SECRET=<your-secret>`

## Environment Variables

- `WORLD_DB` (default local: `world.db`)
- `TICK_SECONDS` (default: `2.0`)
- `MAX_TICKS_PER_REQUEST` (default: `4`)
- `MAX_ACTIONS_PER_SUBMIT` (default: `6`)
- `SESSION_TTL_SECONDS` (default: `86400`)
- `LOCAL_AUTH_ENABLED` (default: `1`)
- `LOCAL_AUTH_SECRET` (default: empty)
- `DEV_MODE` (default: `0`)
- `DEV_TOKEN` (default: `dev`)
- `SERVERLESS_MODE` (default: `1` on Vercel, `0` local)

## Vercel Deploy

Project already includes:
- `api/index.py`
- `vercel.json`
- `requirements.txt`
- `.vercelignore`

Set these env vars in Vercel:
- `SERVERLESS_MODE=1`
- `LOCAL_AUTH_ENABLED=1`
- `TICK_SECONDS=2.0`

Then deploy.

## Notes

- On Vercel, DB is ephemeral by default (temp filesystem).
- For persistent production state, move DB/storage to managed external storage.
