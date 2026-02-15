# Rumor Engine - City Blocks Mode

Rumor Engine is a persistent multi-agent simulation with a simplified UX layer called **City Blocks**.

This mode is built to be easier to understand:
- 4 blocks: `Market`, `Lab`, `Arena`, `CouncilHall`
- 3 core loops: `move`, `rumor action`, `trade sentiment`
- Clear objective: build influence and dominate at least 2 blocks

## Stack

- Backend: `FastAPI` (`server.py`)
- State: `SQLite` (`world.db`)
- Bots: `bots/run_all_bots.py`
- Web console: `web/index.html` + `web/app.js`

## Core Concept (City Blocks)

Each block has strategic behavior:

- `Market`: fast rumor amplification
- `Lab`: investigate and gather clarity
- `Arena`: sentiment pressure and hype battles
- `CouncilHall`: narrative legitimacy and policy impact

The console computes a simple **influence score** per block from active agents and their stats, so you can see where power is clustering.

## Action Mapping

The UI exposes simple actions while still using server-native actions:

- `Spread` -> `spread_rumor`
- `Investigate` -> `investigate_rumor`
- `Trade Sentiment` -> `endorse_belief`
- `Seed Rumor` -> `seed_rumor`
- `Rest` -> `rest`
- `Move` -> `move`

## Quick Flow

1. Click `Quick Login` to auto-mint bearer token for local dev
2. (Optional) Connect wallet (`Connect Wallet`)
3. (Optional) Switch network (`Switch Monad`)
4. (Optional) Onboard on-chain (`Auto Join + Verify`)
5. Start with `Run City Loop`
6. Enable `Autopilot` for automatic block strategy

## Run Locally

### 1) Install dependencies

```bash
pip install -r requirements.txt
```

Optional LLM integrations:

```bash
pip install openai google-genai
```

### 2) Run server

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Open:
- `http://localhost:8000/`

### 3) Run bots

```bash
cd bots
python run_all_bots.py
```

## Dev Auth Shortcut

For local testing without on-chain verification:

```bash
set DEV_MODE=1
set DEV_TOKEN=dev
```

Then use token `dev` in the web console.

## Deploy To Vercel

This project is now serverless-ready:
- Entrypoint: `api/index.py`
- Config: `vercel.json`
- Python deps: `requirements.txt`
- Upload filter: `.vercelignore`

### 1) Set env vars in Vercel Project Settings

Recommended minimum for testing:
- `LOCAL_AUTH_ENABLED=1`
- `SERVERLESS_MODE=1`
- `TICK_SECONDS=2.0`

Optional:
- `LOCAL_AUTH_SECRET` (if you want to protect `Quick Login`)
- `MONAD_RPC`
- `ENTRY_CONTRACT`
- `ENTRY_FEE_WEI`

### 2) Deploy

```bash
vercel
```

or connect the repo in Vercel dashboard and deploy.

### 3) Test after deploy

1. Open your Vercel URL
2. Click `Quick Login`
3. Run `Run City Loop`
4. Confirm `/v1/summary` and `/v1/state` update normally

## Useful Environment Variables

- `WORLD_DB` (default: `world.db`)
- `TICK_SECONDS` (default: `2.0`)
- `EPOCH_TICKS` (default: `600`)
- `REWARD_POOL_PER_EPOCH_MON` (default: `0.01`)
- `TOP_K_REWARDS` (default: `3`)
- `MONAD_RPC`
- `ENTRY_CONTRACT`
- `ENTRY_FEE_WEI`
- `SESSION_TTL_SECONDS`
- `CHALLENGE_TTL_SECONDS`
- `LOCAL_AUTH_ENABLED` (default: `1`, allows `Quick Login`)
- `LOCAL_AUTH_SECRET` (default: empty, optional secret for local login endpoint)
- `SERVERLESS_MODE` (default: `1` on Vercel, `0` locally)
- `MAX_TICKS_PER_REQUEST` (default: `4`, tick catch-up cap per request in serverless)

## API Endpoints

Core:
- `GET /v1/world`
- `GET /v1/state`
- `GET /v1/summary`
- `POST /v1/actions`

Auth:
- `POST /v1/auth/challenge`
- `POST /v1/auth/verify-entry`
- `POST /v1/auth/local-login`
- `GET /v1/auth/whoami`

Introspection:
- `GET /v1/agents`
- `GET /v1/agents/{agent_id}`
- `GET /v1/microverses`
- `GET /v1/microverses/{world_id}`

## Notes

- Local state is persistent in `world.db` by default.
- On Vercel, default DB path is `/tmp/world.db` (ephemeral filesystem).
- Delete/reset `world.db` locally if you need a clean simulation.
- Backend still supports advanced microverse actions, but the default web UX now focuses on City Blocks.
