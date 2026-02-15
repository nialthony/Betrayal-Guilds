# Betrayal Guilds Arena

World-agent arena for the Gaming Agents track:
- Guild PvP (`alpha` vs `omega`)
- Hidden traitor per guild
- Suspicion, trust, and betrayal economy
- Session-authenticated agent actions

## Stack

- Backend engine: `FastAPI` (`server.py`)
- State: SQLite (`ARENA_DB`, default `betrayal_guilds.db`)
- Frontend source: `frontend/` (Vite + React + TypeScript)
- Wallet adapter: `wagmi` + `RainbowKit` (Monad Testnet chain config)
- Frontend build output: `web/` (served by FastAPI)
- Bot runner: `bots/run_all_bots.py`
- Vercel entrypoint: `api/index.py`

## Monad Wallet Setup

Frontend includes Monad Testnet chain:
- Chain ID: `10143`
- RPC: `https://testnet-rpc.monad.xyz`
- Explorer: `https://testnet.monadvision.com`

Wallet login flow:
1. `POST /v1/auth/wallet/challenge`
2. Sign challenge message from connected wallet
3. `POST /v1/auth/wallet/verify` to receive bearer token session

Set WalletConnect project id in `frontend/.env`:

```bash
VITE_WALLETCONNECT_PROJECT_ID=your_project_id
```

Optional API override (for external frontend host):

```bash
VITE_API_BASE=https://your-api-host
```

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
- `POST /v1/auth/wallet/challenge`
- `POST /v1/auth/wallet/verify`
- `GET /v1/auth/whoami`

Admin:
- `POST /v1/admin/reset-world`

## Local Development

1. Install backend deps:

```bash
pip install -r requirements.txt
```

2. Install frontend deps:

```bash
cd frontend
npm install
```

3. Run backend:

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

4. Run Vite dev server (proxy `/v1` -> backend):

```bash
cd frontend
npm run dev
```

Open `http://localhost:5173`.

## Production Build (served by FastAPI)

Build frontend into `web/`:

```bash
cd frontend
npm run build
```

Then open `http://localhost:8000/`.

## Environment Variables (Backend)

- `ARENA_DB` (default: `betrayal_guilds.db`)
- `SERVERLESS_MODE` (`1` on Vercel, `0` local by default)
- `TICK_SECONDS` (default: `2.0`)
- `MAX_TICKS_PER_REQUEST` (default: `4`)
- `MAX_ACTIONS_PER_SUBMIT` (default: `6`)
- `SESSION_TTL_SECONDS` (default: `86400`)
- `WALLET_CHALLENGE_TTL_SECONDS` (default: `300`)
- `LOCAL_AUTH_ENABLED` (default: `1`)
- `LOCAL_AUTH_SECRET` (optional)
- `ADMIN_RESET_SECRET` (optional)
- `DEV_MODE` (default: `0`)
- `DEV_TOKEN` (default: `dev`)

## Vercel Notes

Project is configured with:
- `vercel.json`
- `api/index.py`

Recommended env on Vercel:
- `SERVERLESS_MODE=1`
- `LOCAL_AUTH_ENABLED=1`
- `TICK_SECONDS=2.0`
- `WALLET_CHALLENGE_TTL_SECONDS=300`
- `VITE_WALLETCONNECT_PROJECT_ID` (set in frontend env at build time)

SQLite on serverless filesystem is ephemeral. Use external storage for persistent production world state.
