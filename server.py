from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import asyncio
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional
from web3 import Web3

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

DB_PATH = os.getenv("WORLD_DB", "world.db")
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2.0"))
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
DEV_TOKEN = os.getenv("DEV_TOKEN", "dev")
EPOCH_TICKS = int(os.getenv("EPOCH_TICKS", "600"))  # contoh: 600 ticks
REWARD_POOL_PER_EPOCH_MON = float(os.getenv("REWARD_POOL_PER_EPOCH_MON", "0.01"))  # demo
TOP_K_REWARDS = int(os.getenv("TOP_K_REWARDS", "3"))

LOCATIONS = ["Town", "Market", "Wilderness", "Mine", "Lab", "Arena", "CouncilHall"]

MONAD_TESTNET_RPC = os.getenv("MONAD_RPC", "https://testnet-rpc.monad.xyz")
ENTRY_CONTRACT = os.getenv("ENTRY_CONTRACT", "0x8FbBB672B13eb7e3C81c10f3F918883bb5025384")  # e.g. 0x8FbB...
ENTRY_FEE_WEI = int(os.getenv("ENTRY_FEE_WEI", "1000000000000000"))  # 0.001 MON
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))

ENTRY_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "bytes32", "name": "agentId", "type": "bytes32"},
            {"indexed": True, "internalType": "bytes32", "name": "sessionKey", "type": "bytes32"},
            {"indexed": True, "internalType": "address", "name": "payer", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "amountWei", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "expiresAt", "type": "uint256"},
        ],
        "name": "EntryPaid",
        "type": "event",
    }
]

# -----------------------
# DB helpers
# -----------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def get_latest_event_id(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(event_id), 0) AS m FROM events")
    return int(cur.fetchone()["m"])

def init_db():
    conn = db()
    cur = conn.cursor()
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS epoch (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        epoch_index INTEGER NOT NULL,
        epoch_start_tick INTEGER NOT NULL,
        epoch_end_tick INTEGER NOT NULL,
        reward_pool_mon REAL NOT NULL,
        last_settled_epoch INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS agent_scores (
        epoch_index INTEGER NOT NULL,
        agent_id TEXT NOT NULL,
        points INTEGER NOT NULL,
        spread_count INTEGER NOT NULL,
        debunk_count INTEGER NOT NULL,
        reality_shift_count INTEGER NOT NULL,
        penalty INTEGER NOT NULL,
        PRIMARY KEY (epoch_index, agent_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS rewards (
        epoch_index INTEGER NOT NULL,
        agent_id TEXT NOT NULL,
        rank INTEGER NOT NULL,
        reward_mon REAL NOT NULL,
        points INTEGER NOT NULL,
        PRIMARY KEY (epoch_index, agent_id)
    )
    """)

    # bootstrap singleton row
    cur.execute("SELECT COUNT(*) AS n FROM epoch")
    if int(cur.fetchone()["n"]) == 0:
        cur.execute("""
        INSERT INTO epoch(id, epoch_index, epoch_start_tick, epoch_end_tick, reward_pool_mon, last_settled_epoch)
        VALUES (1, 1, 0, ?, ?, 0)
        """, (EPOCH_TICKS, REWARD_POOL_PER_EPOCH_MON))

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        payer TEXT NOT NULL,
        expires_at_unix INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS used_txs (
        tx_hash TEXT PRIMARY KEY,
        created_at INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        tick INTEGER NOT NULL,
        type TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS snapshots (
        tick INTEGER PRIMARY KEY,
        state TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS actions (
        action_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        tick_submitted INTEGER NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL,
        error TEXT,
        created_at INTEGER NOT NULL
    )
    """)

    # init meta tick if absent
    cur.execute("SELECT value FROM meta WHERE key='tick'")
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO meta(key,value) VALUES('tick','0')")

    # init snapshot if absent
    cur.execute("SELECT tick FROM snapshots ORDER BY tick DESC LIMIT 1")
    snap = cur.fetchone()
    if snap is None:
        state = default_state()
        cur.execute(
            "INSERT INTO snapshots(tick,state,created_at) VALUES(?,?,?)",
            (0, json.dumps(state), int(time.time()))
        )

    conn.commit()
    conn.close()

def get_tick(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key='tick'")
    return int(cur.fetchone()["value"])

def set_tick(conn: sqlite3.Connection, tick: int):
    cur = conn.cursor()
    cur.execute("UPDATE meta SET value=? WHERE key='tick'", (str(tick),))

def latest_snapshot(conn: sqlite3.Connection) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute("SELECT tick,state FROM snapshots ORDER BY tick DESC LIMIT 1")
    row = cur.fetchone()
    return {"tick": int(row["tick"]), "state": json.loads(row["state"])}

def save_snapshot(conn: sqlite3.Connection, tick: int, state: Dict[str, Any]):
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO snapshots(tick,state,created_at) VALUES(?,?,?)",
        (tick, json.dumps(state), int(time.time()))
    )

def insert_event(conn: sqlite3.Connection, tick: int, etype: str, payload: Dict[str, Any]) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO events(tick,type,payload,created_at) VALUES(?,?,?,?)",
        (tick, etype, json.dumps(payload), int(time.time()))
    )
    return cur.lastrowid

def fetch_events_since(conn: sqlite3.Connection, since_event_id: int, limit: int = 500) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT event_id,tick,type,payload FROM events WHERE event_id>? ORDER BY event_id ASC LIMIT ?",
        (since_event_id, limit)
    )
    out = []
    for r in cur.fetchall():
        out.append({
            "event_id": int(r["event_id"]),
            "tick": int(r["tick"]),
            "type": r["type"],
            "payload": json.loads(r["payload"]),
        })
    return out

def queue_action(conn: sqlite3.Connection, action_id: str, agent_id: str, tick_submitted: int, payload: Dict[str, Any]):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO actions(action_id,agent_id,tick_submitted,payload,status,error,created_at) VALUES(?,?,?,?,?,?,?)",
        (action_id, agent_id, tick_submitted, json.dumps(payload), "queued", None, int(time.time()))
    )

def fetch_queued_actions(conn: sqlite3.Connection, tick: int, limit: int = 200) -> List[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        """SELECT action_id,agent_id,tick_submitted,payload
           FROM actions
           WHERE status='queued' AND tick_submitted<=?
           ORDER BY tick_submitted ASC, created_at ASC, agent_id ASC, action_id ASC
           LIMIT ?""",
        (tick, limit)
    )
    return cur.fetchall()

def mark_action(conn: sqlite3.Connection, action_id: str, status: str, error: Optional[str] = None):
    cur = conn.cursor()
    cur.execute("UPDATE actions SET status=?, error=? WHERE action_id=?", (status, error, action_id))

def get_epoch(conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM epoch WHERE id=1")
    return cur.fetchone()

def upsert_score(conn, epoch_index: int, agent_id: str, dp=0, ds=0, dd=0, dr=0, pen=0):
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO agent_scores(epoch_index, agent_id, points, spread_count, debunk_count, reality_shift_count, penalty)
    VALUES (?,?,?,?,?,?,?)
    ON CONFLICT(epoch_index, agent_id) DO UPDATE SET
      points = points + excluded.points,
      spread_count = spread_count + excluded.spread_count,
      debunk_count = debunk_count + excluded.debunk_count,
      reality_shift_count = reality_shift_count + excluded.reality_shift_count,
      penalty = penalty + excluded.penalty
    """, (epoch_index, agent_id, dp, ds, dd, dr, pen))

def top_scores(conn, epoch_index: int, limit: int = 10):
    cur = conn.cursor()
    cur.execute("""
    SELECT agent_id, points, spread_count, debunk_count, reality_shift_count, penalty
    FROM agent_scores
    WHERE epoch_index=?
    ORDER BY points DESC
    LIMIT ?
    """, (epoch_index, limit))
    return cur.fetchall()

def write_reward(conn, epoch_index: int, agent_id: str, rank: int, reward_mon: float, points: int):
    cur = conn.cursor()
    cur.execute("""
    INSERT OR REPLACE INTO rewards(epoch_index, agent_id, rank, reward_mon, points)
    VALUES (?,?,?,?,?)
    """, (epoch_index, agent_id, rank, reward_mon, points))

def get_rewards(conn, epoch_index: int, limit: int = 10):
    cur = conn.cursor()
    cur.execute("""
    SELECT agent_id, rank, reward_mon, points
    FROM rewards
    WHERE epoch_index=?
    ORDER BY rank ASC
    LIMIT ?
    """, (epoch_index, limit))
    return cur.fetchall()

# -----------------------
# World state
# -----------------------
def default_state() -> Dict[str, Any]:
    return {
        "tick": 0,
        "agents": {},  # agent_id -> {location, credits, reputation, inv, stamina}
        "rumors": {},  # rumor_id -> rumor object
        "market": {
            "prices": {"ore": 10, "food": 3, "wood": 4},
        }
    }

def ensure_agent(state: Dict[str, Any], agent_id: str):
    if agent_id not in state["agents"]:
        state["agents"][agent_id] = {
            "location": "Town",
            "credits": 50,
            "reputation": 0,
            "stamina": 10,
            "inv": {}
        }

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def apply_effects_to_market(state: Dict[str, Any]):
    """
    Simple derived effects: rumor effects may shift prices based on belief.
    We recompute each tick as a small drift from top-believed rumors.
    """
    base_prices = {"ore": 10, "food": 3, "wood": 4}
    prices = dict(base_prices)

    # accumulate effects: if belief high, apply more
    for r in state["rumors"].values():
        belief = float(r.get("belief_score", 0.0))
        effects = r.get("effects", {}) or {}
        strength = clamp(belief * 5, 0.0, 1.0)

        # supported effect keys: "<item>.price"
        for k, v in effects.items():
            if k.endswith(".price"):
                item = k.split(".")[0]
                try:
                    prices[item] = prices.get(item, base_prices.get(item, 5)) + float(v) * strength
                except Exception:
                    pass

    # clamp and round nicely
    for item in list(prices.keys()):
        prices[item] = int(clamp(prices[item], 1, 10_000))

    state["market"]["prices"] = prices

# -----------------------
# Simulation: apply actions
# -----------------------
def apply_action(state: Dict[str, Any], tick: int, agent_id: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Returns list of events to emit.
    """
    ensure_agent(state, agent_id)
    a = state["agents"][agent_id]
    t = payload.get("type")
    events = []

    def spend_stamina(cost: int) -> bool:
        if a["stamina"] < cost:
            return False
        a["stamina"] -= cost
        return True

    if t == "move":
        dest = payload.get("to")
        if dest not in LOCATIONS:
            raise ValueError("invalid_location")
        a["location"] = dest
        events.append({"type": "agent_moved", "payload": {"agent_id": agent_id, "to": dest}})
        return events

    if t == "seed_rumor":
        if not spend_stamina(1):
            raise ValueError("no_stamina")
        claim = str(payload.get("claim", "")).strip()[:200]
        effects = payload.get("effects", {}) or {}
        rumor_id = f"r_{uuid.uuid4().hex[:8]}"

        state["rumors"][rumor_id] = {
            "rumor_id": rumor_id,
            "claim": claim or "Unnamed rumor",
            "originator": agent_id,
            "belief_score": 0.10,      # starts small
            "spread_count": 0,
            "debunk_count": 0,
            "effects": effects,
            "created_at_tick": tick,
        }

        events.append({
            "type": "rumor_created",
            "payload": {"rumor_id": rumor_id, "claim": state["rumors"][rumor_id]["claim"], "effects": effects}
        })
        return events

    if t == "spread_rumor":
        if not spend_stamina(1):
            raise ValueError("no_stamina")
        rid = payload.get("rumor_id")
        effort = int(payload.get("effort", 1))
        if rid not in state["rumors"]:
            raise ValueError("rumor_not_found")
        r = state["rumors"][rid]
        r["spread_count"] += max(1, effort)
        # belief increases with effort + reputation (small)
        boost = 0.08 * effort + 0.003 * a["reputation"]
        r["belief_score"] = clamp(r["belief_score"] + boost, 0.0, 1.0)
        events.append({"type": "rumor_spread", "payload": {"agent_id": agent_id, "rumor_id": rid, "effort": effort}})
        return events

    if t == "endorse_belief":
        rid = payload.get("rumor_id")
        if rid not in state["rumors"]:
            raise ValueError("rumor_not_found")
        r = state["rumors"][rid]
        r["belief_score"] = clamp(r["belief_score"] + 0.01, 0.0, 1.0)
        events.append({"type": "rumor_endorsed", "payload": {"agent_id": agent_id, "rumor_id": rid}})
        return events

    if t == "investigate_rumor":
        if not spend_stamina(2):
            raise ValueError("no_stamina")
        rid = payload.get("rumor_id")
        if rid not in state["rumors"]:
            raise ValueError("rumor_not_found")
        # mint evidence token
        tok = f"e_{uuid.uuid4().hex[:8]}"
        # store evidence on agent
        inv = a["inv"]
        inv["evidence"] = inv.get("evidence", 0) + 1
        # small rep gain
        a["reputation"] += 1
        events.append({"type": "evidence_created", "payload": {"rumor_id": rid, "evidence_token": tok, "agent_id": agent_id}})
        return events

    if t == "debunk":
        rid = payload.get("rumor_id")
        if rid not in state["rumors"]:
            raise ValueError("rumor_not_found")
        r = state["rumors"][rid]
        # debunk reduces belief
        r["debunk_count"] += 1
        r["belief_score"] = clamp(r["belief_score"] - 0.08, 0.0, 1.0)
        a["reputation"] += 2
        events.append({"type": "rumor_debunked", "payload": {"agent_id": agent_id, "rumor_id": rid}})
        return events

    if t == "fabricate_evidence":
        if not spend_stamina(2):
            raise ValueError("no_stamina")
        rid = payload.get("rumor_id")
        if rid not in state["rumors"]:
            raise ValueError("rumor_not_found")
        # risky: sometimes backfires deterministically based on tick+agent hash
        h = (hash(agent_id) + tick) % 10
        if h < 2:
            # caught
            a["reputation"] = max(0, a["reputation"] - 5)
            events.append({"type": "fabrication_caught", "payload": {"agent_id": agent_id, "rumor_id": rid}})
        else:
            # “successful” fabrication boosts belief a bit
            state["rumors"][rid]["belief_score"] = clamp(state["rumors"][rid]["belief_score"] + 0.05, 0.0, 1.0)
            events.append({"type": "fabrication_success", "payload": {"agent_id": agent_id, "rumor_id": rid}})
        return events

    # ignore unknown actions (or you can reject)
    raise ValueError("unknown_action_type")

def tick_decay(state: Dict[str, Any]):
    """
    Natural decay so rumors don't stick forever. Also stamina regen.
    """
    # stamina regen
    for a in state["agents"].values():
        a["stamina"] = min(10, a["stamina"] + 1)

    # rumor decay
    to_del = []
    for rid, r in state["rumors"].items():
        r["belief_score"] = clamp(r["belief_score"] - 0.005, 0.0, 1.0)
        # prune dead rumors after a while
        if r["belief_score"] <= 0.0 and (state["tick"] - r["created_at_tick"]) > 80:
            to_del.append(rid)
    for rid in to_del:
        del state["rumors"][rid]

def threshold_events(state: Dict[str, Any], tick: int) -> List[Dict[str, Any]]:
    evs = []
    for r in state["rumors"].values():
        b = float(r.get("belief_score", 0.0))
        flags = r.setdefault("flags", {})

        if b >= 0.20 and not flags.get("soft"):
            flags["soft"] = True
            evs.append({
                "type": "rumor_threshold_soft",
                "payload": {
                    "rumor_id": r["rumor_id"],
                    "belief": round(b, 3),
                    "claim": r.get("claim", "")
                }
            })

        if b >= 0.45 and not flags.get("hard"):
            flags["hard"] = True
            evs.append({
                "type": "rumor_threshold_hard",
                "payload": {
                    "rumor_id": r["rumor_id"],
                    "belief": round(b, 3),
                    "claim": r.get("claim", "")
                }
            })
            # 🔥 Dramatic event for judge demo
            evs.append({
                "type": "REALITY_SHIFT",
                "payload": {
                    "rumor_id": r["rumor_id"],
                    "belief": round(b, 3),
                    "claim": r.get("claim", ""),
                    "message": "Collective belief has altered reality."
                }
            })

    return evs

# -----------------------
# API models
# -----------------------
class VerifyEntryRequest(BaseModel):
    tx_hash: str

class VerifyEntryResponse(BaseModel):
    access_token: str
    expires_at_unix: int

class SubmitActionsRequest(BaseModel):
    agent_id: str
    tick_submitted: int = 0
    actions: List[Dict[str, Any]] = Field(default_factory=list)

# -----------------------
# Auth
# -----------------------
def require_auth(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="missing_authorization")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="invalid_authorization")

    token = authorization.split(" ", 1)[1].strip()

    # DEV shortcut
    if DEV_MODE:
        if token != DEV_TOKEN:
            raise HTTPException(status_code=403, detail="bad_token")
        return "dev_agent"

    # Token-gated mode: validate session token from DB
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT agent_id, expires_at_unix FROM sessions WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=403, detail="invalid_session_token")
        if int(row["expires_at_unix"]) < int(time.time()):
            raise HTTPException(status_code=403, detail="session_expired")
        return row["agent_id"]
    finally:
        conn.close()

# -----------------------
# FastAPI app
# -----------------------
app = FastAPI(title="Rumor Engine World Server")

# Serve cyber terminal web UI
app.mount("/web", StaticFiles(directory="web"), name="web")

@app.get("/")
def home():
    # loads web/index.html
    return FileResponse("web/index.html")

@app.on_event("startup")
async def startup():
    init_db()
    app.state.runner_task = asyncio.create_task(tick_runner())

@app.on_event("shutdown")
async def shutdown():
    task = getattr(app.state, "runner_task", None)
    if task:
        task.cancel()

@app.get("/v1/world")
def world_info():
    # keep this consistent with your actual on-chain fee
    entry_fee_mon = float(ENTRY_FEE_WEI) / 1e18
    return {
        "tick_seconds": TICK_SECONDS,
        "entry_fee_mon": entry_fee_mon,  # e.g. 0.001
        "entry_contract": ENTRY_CONTRACT,
        "rpc": MONAD_TESTNET_RPC,
        "locations": LOCATIONS,
        "rules_hash": "rumor-engine-v1"
    }

@app.get("/v1/state")
def get_state(since_event_id: Optional[int] = None):
    conn = db()
    try:
        snap_row = latest_snapshot(conn)
        snap = snap_row["state"] if snap_row else None

        # Always return snapshot + latest_event_id for dashboards
        if since_event_id is None:
            latest_id = get_latest_event_id(conn)  # you may need to implement this helper (see below)
            return {"snapshot": snap, "events": [], "latest_event_id": latest_id}

        events = fetch_events_since(conn, since_event_id)
        latest_id = events[-1]["event_id"] if events else since_event_id
        return {"snapshot": snap, "events": events, "latest_event_id": latest_id}
    finally:
        conn.close()

@app.post("/v1/actions")
def submit_actions(req: SubmitActionsRequest, authorization: Optional[str] = Header(default=None)):
    authed_agent = require_auth(authorization)

    # optional strict mode: enforce caller identity matches request agent_id
    if not DEV_MODE and req.agent_id != authed_agent:
        raise HTTPException(status_code=403, detail="agent_id_mismatch")

    if len(req.actions) > 10:
        raise HTTPException(status_code=400, detail="too_many_actions_max_10")

    conn = db()
    try:
        action_ids = []
        for payload in req.actions:
            aid = str(uuid.uuid4())
            queue_action(conn, aid, req.agent_id, int(req.tick_submitted), payload)
            action_ids.append(aid)
        conn.commit()
        return {"accepted": True, "action_ids": action_ids}
    finally:
        conn.close()

@app.get("/v1/summary")
def summary():
    conn = db()
    try:
        snap = latest_snapshot(conn)["state"]

        rumors = list(snap["rumors"].values())
        rumors.sort(key=lambda r: float(r.get("belief_score", 0.0)), reverse=True)
        top = rumors[:5]

        return {
            "tick": snap.get("tick", 0),
            "market_prices": snap["market"]["prices"],
            "top_rumors": [
                {
                    "rumor_id": r["rumor_id"],
                    "belief": round(float(r.get("belief_score", 0.0)), 3),
                    "claim": r.get("claim", ""),
                    "effects": r.get("effects", {}),
                    "flags": r.get("flags", {}),
                    "originator": r.get("originator", ""),
                    "spread_count": r.get("spread_count", 0),
                    "debunk_count": r.get("debunk_count", 0),
                }
                for r in top
            ],
            "agents": {
                aid: {
                    "location": a.get("location", ""),
                    "reputation": a.get("reputation", 0),
                    "stamina": a.get("stamina", 0),
                    "credits": a.get("credits", 0),
                }
                for aid, a in (snap.get("agents", {}) or {}).items()
            }
        }
    finally:
        conn.close()

@app.post("/v1/auth/verify-entry", response_model=VerifyEntryResponse)
def verify_entry(req: VerifyEntryRequest):
    # DEV shortcut (optional)
    if DEV_MODE:
        return VerifyEntryResponse(access_token=DEV_TOKEN, expires_at_unix=int(time.time()) + 3600)

    if not ENTRY_CONTRACT:
        raise HTTPException(status_code=500, detail="ENTRY_CONTRACT not configured")

    tx_hash = (req.tx_hash or "").strip()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise HTTPException(status_code=400, detail="invalid_tx_hash")

    w3 = Web3(Web3.HTTPProvider(MONAD_TESTNET_RPC))
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(ENTRY_CONTRACT),
        abi=ENTRY_ABI
    )

    conn = db()
    try:
        cur = conn.cursor()

        # prevent replay
        cur.execute("SELECT tx_hash FROM used_txs WHERE tx_hash=?", (tx_hash,))
        if cur.fetchone() is not None:
            raise HTTPException(status_code=400, detail="tx_hash_already_used")

        # receipt
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            raise HTTPException(status_code=400, detail="tx_not_found_or_not_final")

        if receipt is None or int(receipt.get("status", 0)) != 1:
            raise HTTPException(status_code=400, detail="tx_failed")

        # decode EntryPaid logs
        try:
            decoded = contract.events.EntryPaid().process_receipt(receipt)
        except Exception:
            decoded = []

        if not decoded:
            raise HTTPException(status_code=400, detail="no_entrypaid_event")

        ev = decoded[0]["args"]
        amountWei = int(ev["amountWei"])
        expiresAt = int(ev["expiresAt"])
        payer = str(ev["payer"])

        if amountWei < ENTRY_FEE_WEI:
            raise HTTPException(status_code=400, detail="insufficient_fee")

        agent_id = "agent_" + ev["agentId"].hex()[:8]  # pendek & unik
        now = int(time.time())

        token = "sess_" + uuid.uuid4().hex
        expires_at_unix = min(expiresAt, now + SESSION_TTL_SECONDS)

        cur.execute("INSERT INTO used_txs(tx_hash, created_at) VALUES(?,?)", (tx_hash, now))
        cur.execute(
            "INSERT INTO sessions(token, agent_id, payer, expires_at_unix, created_at) VALUES(?,?,?,?,?)",
            (token, agent_id, payer, expires_at_unix, now)
        )
        conn.commit()

        return VerifyEntryResponse(access_token=token, expires_at_unix=expires_at_unix)
    finally:
        conn.close()

@app.get("/v1/epoch")
def epoch_status():
    conn = db()
    try:
        ep = get_epoch(conn)
        return {
            "epoch_index": ep["epoch_index"],
            "epoch_start_tick": ep["epoch_start_tick"],
            "epoch_end_tick": ep["epoch_end_tick"],
            "reward_pool_mon": ep["reward_pool_mon"],
        }
    finally:
        conn.close()

@app.get("/v1/leaderboard")
def leaderboard(epoch_index: Optional[int] = None, limit: int = 10):
    conn = db()
    try:
        ep = get_epoch(conn)
        eidx = int(epoch_index or ep["epoch_index"])
        rows = top_scores(conn, eidx, limit=limit)
        return {
            "epoch_index": eidx,
            "leaders": [
                {
                    "agent_id": r["agent_id"],
                    "points": int(r["points"]),
                    "spread_count": int(r["spread_count"]),
                    "debunk_count": int(r["debunk_count"]),
                    "reality_shift_count": int(r["reality_shift_count"]),
                    "penalty": int(r["penalty"]),
                }
                for r in rows
            ],
            "rewards": [
                {"agent_id": r["agent_id"], "rank": int(r["rank"]), "reward_mon": float(r["reward_mon"]), "points": int(r["points"])}
                for r in get_rewards(conn, eidx, limit=TOP_K_REWARDS)
            ]
        }
    finally:
        conn.close()

@app.get("/v1/auth/whoami")
def whoami(authorization: Optional[str] = Header(default=None)):
    agent_id = require_auth(authorization)

    if DEV_MODE:
        return {
            "mode": "dev",
            "agent_id": agent_id,
            "token_prefix": (DEV_TOKEN[:12] + "...") if DEV_TOKEN else None,
        }

    token = authorization.split(" ", 1)[1].strip()
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT agent_id, payer, expires_at_unix, created_at FROM sessions WHERE token=?",
            (token,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=403, detail="invalid_session_token")
        return {
            "mode": "session",
            "agent_id": row["agent_id"],
            "payer": row["payer"],
            "expires_at_unix": int(row["expires_at_unix"]),
            "created_at_unix": int(row["created_at"]),
            "now_unix": int(time.time()),
        }
    finally:
        conn.close()

# -----------------------
# Tick loop
# -----------------------
SNAPSHOT_EVERY = 10

async def tick_runner():
    while True:
        try:
            run_one_tick()
        except Exception as e:
            # Keep server alive even if one tick fails
            print("Tick error:", repr(e))
        await asyncio.sleep(TICK_SECONDS)

def settle_epoch_if_needed(conn, current_tick: int):
    ep = get_epoch(conn)
    if current_tick < int(ep["epoch_end_tick"]):
        return

    epoch_index = int(ep["epoch_index"])
    pool = float(ep["reward_pool_mon"])

    winners = top_scores(conn, epoch_index, limit=TOP_K_REWARDS)
    if not winners:
        # advance epoch anyway
        pass
    else:
        # simple split: 50/30/20 (atau adapt by TOP_K_REWARDS)
        splits = [0.5, 0.3, 0.2][:TOP_K_REWARDS]
        while len(splits) < min(TOP_K_REWARDS, len(winners)):
            splits.append(0.0)

        for i, row in enumerate(winners[:TOP_K_REWARDS]):
            reward = pool * splits[i]
            write_reward(conn, epoch_index, row["agent_id"], i+1, reward, int(row["points"]))

    # emit events (biar keren di log)
    emit_event(conn, "epoch_ended", {"epoch_index": epoch_index, "reward_pool_mon": pool, "tick": current_tick})
    settle_epoch_if_needed(conn, current_tick=snap["tick"])
    emit_event(conn, "rewards_assigned", {"epoch_index": epoch_index, "winners": [
        {"agent_id": r["agent_id"], "rank": i+1, "points": int(r["points"])}
        for i, r in enumerate(winners[:TOP_K_REWARDS])
    ]})

    # advance epoch
    new_epoch_index = epoch_index + 1
    start_tick = current_tick
    end_tick = current_tick + EPOCH_TICKS
    cur = conn.cursor()
    cur.execute("""
    UPDATE epoch
    SET epoch_index=?, epoch_start_tick=?, epoch_end_tick=?, reward_pool_mon=?, last_settled_epoch=?
    WHERE id=1
    """, (new_epoch_index, start_tick, end_tick, REWARD_POOL_PER_EPOCH_MON, epoch_index))
    conn.commit()

def run_one_tick():
    conn = db()
    try:
        tick = get_tick(conn)
        snap = latest_snapshot(conn)
        state = snap["state"]
        state["tick"] = tick

        queued = fetch_queued_actions(conn, tick)

        # Apply queued actions deterministically
        for row in queued:
            aid = row["action_id"]
            agent_id = row["agent_id"]
            payload = json.loads(row["payload"])
            try:
                evs = apply_action(state, tick, agent_id, payload)
                for ev in evs:
                    insert_event(conn, tick, ev["type"], ev["payload"])
                mark_action(conn, aid, "applied", None)
            except Exception as e:
                mark_action(conn, aid, "rejected", str(e))

        # natural dynamics
        tick_decay(state)
        apply_effects_to_market(state)
        
        for ev in threshold_events(state, tick):
            insert_event(conn, tick, ev["type"], ev["payload"])

        # advance tick
        tick += 1
        state["tick"] = tick
        set_tick(conn, tick)

        if tick % SNAPSHOT_EVERY == 0:
            save_snapshot(conn, tick, state)

        conn.commit()
    finally:

        conn.close()
