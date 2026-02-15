from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import asyncio
import hashlib
import json
import os
import random
import sqlite3
import threading
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional
from web3 import Web3
from eth_account.messages import encode_defunct

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
IS_VERCEL = (os.getenv("VERCEL", "").lower() in ("1", "true", "yes")) or bool(os.getenv("VERCEL_ENV"))
SERVERLESS_MODE = os.getenv("SERVERLESS_MODE", "1" if IS_VERCEL else "0") == "1"
DB_PATH_DEFAULT = os.path.join(tempfile.gettempdir(), "world.db") if IS_VERCEL else "world.db"
DB_PATH = os.getenv("WORLD_DB", DB_PATH_DEFAULT)
TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2.0"))
MAX_TICKS_PER_REQUEST = int(os.getenv("MAX_TICKS_PER_REQUEST", "4"))
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
DEV_TOKEN = os.getenv("DEV_TOKEN", "dev")
LOCAL_AUTH_ENABLED = os.getenv("LOCAL_AUTH_ENABLED", "1") == "1"
LOCAL_AUTH_SECRET = os.getenv("LOCAL_AUTH_SECRET", "")
EPOCH_TICKS = int(os.getenv("EPOCH_TICKS", "600"))  # contoh: 600 ticks
REWARD_POOL_PER_EPOCH_MON = float(os.getenv("REWARD_POOL_PER_EPOCH_MON", "0.01"))  # demo
TOP_K_REWARDS = int(os.getenv("TOP_K_REWARDS", "3"))
TICK_LOCK = threading.Lock()

LOCATIONS = ["Town", "Market", "Wilderness", "Mine", "Lab", "Arena", "CouncilHall"]

MONAD_TESTNET_RPC = os.getenv("MONAD_RPC", "https://testnet-rpc.monad.xyz")
ENTRY_CONTRACT = os.getenv("ENTRY_CONTRACT", "0x8FbBB672B13eb7e3C81c10f3F918883bb5025384")  # e.g. 0x8FbB...
ENTRY_FEE_WEI = int(os.getenv("ENTRY_FEE_WEI", "1000000000000000"))  # 0.001 MON
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
CHALLENGE_TTL_SECONDS = int(os.getenv("CHALLENGE_TTL_SECONDS", "300"))

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
    CREATE TABLE IF NOT EXISTS auth_challenges (
        challenge_id TEXT PRIMARY KEY,
        payer TEXT NOT NULL,
        nonce TEXT NOT NULL,
        issued_at_unix INTEGER NOT NULL,
        expires_at_unix INTEGER NOT NULL,
        used INTEGER NOT NULL DEFAULT 0
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

    cur.execute("SELECT value FROM meta WHERE key='last_tick_unix'")
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO meta(key,value) VALUES('last_tick_unix',?)", (str(int(time.time())),))

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

def get_meta_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    if row is None:
        return int(default)
    try:
        return int(row["value"])
    except Exception:
        return int(default)

def set_meta_value(conn: sqlite3.Connection, key: str, value: str):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )

def queued_action_count(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS n FROM actions WHERE status='queued'")
    return int(cur.fetchone()["n"])

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

def normalize_payer_address(raw: str) -> str:
    if not raw:
        raise HTTPException(status_code=400, detail="missing_payer")
    try:
        return Web3.to_checksum_address(raw).lower()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_payer_address")

def build_challenge_message(challenge_id: str, payer: str, nonce: str) -> str:
    # Keep message deterministic so signer and verifier hash the same payload.
    return (
        "Rumor Engine Verify Entry\n"
        f"challenge_id:{challenge_id}\n"
        f"payer:{payer}\n"
        f"nonce:{nonce}"
    )

def create_auth_challenge(conn: sqlite3.Connection, payer: str) -> Dict[str, Any]:
    now = int(time.time())
    challenge_id = "ch_" + uuid.uuid4().hex
    nonce = hashlib.sha256(f"{challenge_id}:{now}:{uuid.uuid4().hex}".encode("utf-8")).hexdigest()[:24]
    expires_at_unix = now + CHALLENGE_TTL_SECONDS
    message = build_challenge_message(challenge_id, payer, nonce)

    cur = conn.cursor()
    cur.execute(
        """INSERT INTO auth_challenges(challenge_id, payer, nonce, issued_at_unix, expires_at_unix, used)
           VALUES(?,?,?,?,?,0)""",
        (challenge_id, payer, nonce, now, expires_at_unix),
    )
    return {
        "challenge_id": challenge_id,
        "message": message,
        "expires_at_unix": expires_at_unix,
    }

def get_auth_challenge(conn: sqlite3.Connection, challenge_id: str) -> Optional[sqlite3.Row]:
    cur = conn.cursor()
    cur.execute(
        "SELECT challenge_id, payer, nonce, expires_at_unix, used FROM auth_challenges WHERE challenge_id=?",
        (challenge_id,),
    )
    return cur.fetchone()

def mark_auth_challenge_used(conn: sqlite3.Connection, challenge_id: str):
    cur = conn.cursor()
    cur.execute("UPDATE auth_challenges SET used=1 WHERE challenge_id=?", (challenge_id,))

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
WORLD_RULES_VERSION = "world-agent-v3-microverse"
MAX_AGENT_ACTION_MEMORY = 24
MAX_AGENT_KNOWN_RUMORS = 120
MICROVERSE_MAX_MEMBERS = 24
MICROVERSE_COLLAPSE_STABILITY = 0.05

AGENT_ARCHETYPES: Dict[str, Dict[str, Any]] = {
    "conspiracist": {
        "spread_bonus": 0.24,
        "debunk_bonus": -0.08,
        "investigation_bonus": 0.04,
        "fabrication_skill": 0.18,
        "recovery": 1,
        "max_stamina": 11,
        "base_credits": 55,
        "base_reputation": 0,
    },
    "investigator": {
        "spread_bonus": -0.05,
        "debunk_bonus": 0.34,
        "investigation_bonus": 0.40,
        "fabrication_skill": -0.20,
        "recovery": 1,
        "max_stamina": 10,
        "base_credits": 50,
        "base_reputation": 1,
    },
    "manipulator": {
        "spread_bonus": 0.11,
        "debunk_bonus": -0.03,
        "investigation_bonus": 0.10,
        "fabrication_skill": 0.42,
        "recovery": 1,
        "max_stamina": 10,
        "base_credits": 52,
        "base_reputation": 0,
    },
    "drifter": {
        "spread_bonus": 0.0,
        "debunk_bonus": 0.0,
        "investigation_bonus": 0.0,
        "fabrication_skill": 0.0,
        "recovery": 1,
        "max_stamina": 10,
        "base_credits": 50,
        "base_reputation": 0,
    },
}

def infer_agent_archetype(agent_id: str) -> str:
    aid = (agent_id or "").lower()
    if "invest" in aid:
        return "investigator"
    if "manip" in aid:
        return "manipulator"
    if "conspir" in aid or "rumor" in aid:
        return "conspiracist"
    return "drifter"

def archetype_profile(agent_id: str) -> Dict[str, Any]:
    name = infer_agent_archetype(agent_id)
    profile = dict(AGENT_ARCHETYPES.get(name, AGENT_ARCHETYPES["drifter"]))
    profile["name"] = name
    return profile

def make_default_agent(agent_id: str) -> Dict[str, Any]:
    arc = archetype_profile(agent_id)
    return {
        "agent_id": agent_id,
        "location": "Town",
        "credits": int(arc["base_credits"]),
        "reputation": int(arc["base_reputation"]),
        "stamina": int(arc["max_stamina"]),
        "inv": {"evidence": 0, "evidence_tokens": {}},
        "identity": {
            "archetype": arc["name"],
            "faction": f"{arc['name']}_guild",
            "home": "Town",
        },
        "attributes": {
            "spread_bonus": float(arc["spread_bonus"]),
            "debunk_bonus": float(arc["debunk_bonus"]),
            "investigation_bonus": float(arc["investigation_bonus"]),
            "fabrication_skill": float(arc["fabrication_skill"]),
            "recovery": int(arc["recovery"]),
            "max_stamina": int(arc["max_stamina"]),
        },
        "memory": {
            "known_rumors": [],
            "focus": None,
            "last_actions": [],
        },
        "world_id": None,
    }

def normalize_agent(state: Dict[str, Any], agent_id: str):
    agent = state["agents"].get(agent_id)
    if agent is None:
        state["agents"][agent_id] = make_default_agent(agent_id)
        return

    # Backward compatibility for old snapshots:
    # old shape had only location/credits/reputation/stamina/inv.
    base = make_default_agent(agent_id)
    if "location" not in agent:
        agent["location"] = base["location"]
    if "credits" not in agent:
        agent["credits"] = base["credits"]
    if "reputation" not in agent:
        agent["reputation"] = base["reputation"]
    if "stamina" not in agent:
        agent["stamina"] = base["stamina"]
    if "inv" not in agent or not isinstance(agent.get("inv"), dict):
        agent["inv"] = {}

    inv = agent["inv"]
    if "evidence" not in inv:
        inv["evidence"] = 0
    if "evidence_tokens" not in inv or not isinstance(inv.get("evidence_tokens"), dict):
        inv["evidence_tokens"] = {}

    if "identity" not in agent or not isinstance(agent.get("identity"), dict):
        agent["identity"] = base["identity"]
    else:
        agent["identity"].setdefault("archetype", base["identity"]["archetype"])
        agent["identity"].setdefault("faction", base["identity"]["faction"])
        agent["identity"].setdefault("home", base["identity"]["home"])

    if "attributes" not in agent or not isinstance(agent.get("attributes"), dict):
        agent["attributes"] = base["attributes"]
    else:
        for k, v in base["attributes"].items():
            agent["attributes"].setdefault(k, v)

    if "memory" not in agent or not isinstance(agent.get("memory"), dict):
        agent["memory"] = base["memory"]
    else:
        agent["memory"].setdefault("known_rumors", [])
        agent["memory"].setdefault("focus", None)
        agent["memory"].setdefault("last_actions", [])
    if "world_id" not in agent:
        agent["world_id"] = None

    max_stamina = int(agent["attributes"].get("max_stamina", 10))
    agent["stamina"] = int(clamp(float(agent["stamina"]), 0, max_stamina))
    agent["credits"] = int(agent["credits"])
    agent["reputation"] = int(agent["reputation"])

def make_default_microverse(world_id: str, owner: str, title: str, entry_fee: int, capacity: int, tick: int) -> Dict[str, Any]:
    return {
        "world_id": world_id,
        "owner": owner,
        "title": title or f"Microverse {world_id[-4:]}",
        "entry_fee": int(max(1, min(50, entry_fee))),
        "capacity": int(max(2, min(MICROVERSE_MAX_MEMBERS, capacity))),
        "members": [owner],
        "treasury_credits": 0,
        "stability": 1.0,
        "anomaly_level": 0.1,
        "created_at_tick": int(tick),
        "last_active_tick": int(tick),
        "history": [{"tick": int(tick), "event": "created", "by": owner}],
    }

def normalize_microverse(state: Dict[str, Any], world_id: str):
    worlds = state.setdefault("microverses", {})
    w = worlds.get(world_id)
    if w is None:
        return
    w.setdefault("world_id", world_id)
    w.setdefault("owner", "")
    w.setdefault("title", f"Microverse {world_id[-4:]}")
    w["entry_fee"] = int(max(1, min(50, int(w.get("entry_fee", 1)))))
    w["capacity"] = int(max(2, min(MICROVERSE_MAX_MEMBERS, int(w.get("capacity", 4)))))
    w.setdefault("members", [])
    w["members"] = [m for m in list(w["members"]) if m in (state.get("agents") or {})]
    w["treasury_credits"] = int(max(0, int(w.get("treasury_credits", 0))))
    w["stability"] = float(clamp(float(w.get("stability", 1.0)), 0.0, 1.25))
    w["anomaly_level"] = float(clamp(float(w.get("anomaly_level", 0.1)), 0.0, 2.0))
    w.setdefault("created_at_tick", int(state.get("tick", 0)))
    w.setdefault("last_active_tick", int(state.get("tick", 0)))
    w.setdefault("history", [])

def ensure_world_schema(state: Dict[str, Any]):
    state.setdefault("tick", 0)
    state.setdefault("agents", {})
    state.setdefault("rumors", {})
    state.setdefault("market", {})
    state["market"].setdefault("prices", {"ore": 10, "food": 3, "wood": 4})
    state.setdefault("microverses", {})
    state.setdefault("world", {})
    state["world"].setdefault("rules_version", WORLD_RULES_VERSION)
    state["world"].setdefault("regions", list(LOCATIONS))
    state["world"].setdefault("agent_protocol", "archetype-memory-economy-microverse")
    state["world"].setdefault("factions", ["conspiracist_guild", "investigator_guild", "manipulator_guild", "drifter_guild"])
    state["world"].setdefault("mode", "microverse-protocol")

    for aid in list((state.get("agents") or {}).keys()):
        normalize_agent(state, aid)
    for wid in list((state.get("microverses") or {}).keys()):
        normalize_microverse(state, wid)

def default_state() -> Dict[str, Any]:
    state = {
        "tick": 0,
        "world": {
            "rules_version": WORLD_RULES_VERSION,
            "regions": list(LOCATIONS),
            "agent_protocol": "archetype-memory-economy-microverse",
            "mode": "microverse-protocol",
            "factions": ["conspiracist_guild", "investigator_guild", "manipulator_guild", "drifter_guild"],
        },
        "agents": {},
        "rumors": {},
        "microverses": {},
        "market": {
            "prices": {"ore": 10, "food": 3, "wood": 4},
        },
    }
    ensure_world_schema(state)
    return state

def ensure_agent(state: Dict[str, Any], agent_id: str):
    ensure_world_schema(state)
    if agent_id not in state["agents"]:
        state["agents"][agent_id] = make_default_agent(agent_id)
    else:
        normalize_agent(state, agent_id)

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def agent_attr(agent: Dict[str, Any], key: str, default: float = 0.0) -> float:
    attrs = agent.get("attributes", {}) or {}
    try:
        return float(attrs.get(key, default))
    except Exception:
        return float(default)

def remember_rumor(agent: Dict[str, Any], rumor_id: str):
    if not rumor_id:
        return
    mem = agent.setdefault("memory", {})
    known = mem.setdefault("known_rumors", [])
    if rumor_id in known:
        return
    known.append(rumor_id)
    if len(known) > MAX_AGENT_KNOWN_RUMORS:
        del known[0:len(known) - MAX_AGENT_KNOWN_RUMORS]

def remember_action(agent: Dict[str, Any], tick: int, action_type: str, payload: Dict[str, Any]):
    mem = agent.setdefault("memory", {})
    hist = mem.setdefault("last_actions", [])
    hist.append({
        "tick": int(tick),
        "type": str(action_type),
        "target": (payload or {}).get("rumor_id"),
    })
    if len(hist) > MAX_AGENT_ACTION_MEMORY:
        del hist[0:len(hist) - MAX_AGENT_ACTION_MEMORY]
    if (payload or {}).get("rumor_id"):
        mem["focus"] = payload["rumor_id"]
        remember_rumor(agent, payload["rumor_id"])

def current_world(state: Dict[str, Any], agent_id: str) -> Optional[Dict[str, Any]]:
    a = (state.get("agents") or {}).get(agent_id)
    if not a:
        return None
    wid = a.get("world_id")
    if not wid:
        return None
    return (state.get("microverses") or {}).get(wid)

def remove_agent_from_world(state: Dict[str, Any], agent_id: str):
    a = (state.get("agents") or {}).get(agent_id)
    if not a:
        return
    wid = a.get("world_id")
    if not wid:
        return
    w = (state.get("microverses") or {}).get(wid)
    if w:
        w["members"] = [m for m in (w.get("members") or []) if m != agent_id]
        w["last_active_tick"] = int(state.get("tick", 0))
    a["world_id"] = None

def add_agent_to_world(state: Dict[str, Any], agent_id: str, world_id: str):
    ensure_agent(state, agent_id)
    w = (state.get("microverses") or {}).get(world_id)
    if w is None:
        raise ValueError("world_not_found")
    if agent_id in (w.get("members") or []):
        return
    remove_agent_from_world(state, agent_id)
    w.setdefault("members", []).append(agent_id)
    w["last_active_tick"] = int(state.get("tick", 0))
    state["agents"][agent_id]["world_id"] = world_id

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

    if t == "create_microverse":
        if not spend_stamina(2):
            raise ValueError("no_stamina")
        title = str(payload.get("title", "")).strip()[:80] or f"{agent_id}'s Weirdverse"
        entry_fee = int(payload.get("entry_fee", 3))
        capacity = int(payload.get("capacity", 6))

        if a["credits"] < 15:
            raise ValueError("insufficient_credits")
        for w in (state.get("microverses") or {}).values():
            if w.get("owner") == agent_id and float(w.get("stability", 0.0)) > MICROVERSE_COLLAPSE_STABILITY:
                raise ValueError("owner_world_exists")

        a["credits"] -= 15
        world_id = f"mv_{uuid.uuid4().hex[:8]}"
        state["microverses"][world_id] = make_default_microverse(world_id, agent_id, title, entry_fee, capacity, tick)
        add_agent_to_world(state, agent_id, world_id)
        remember_action(a, tick, t, {"world_id": world_id})
        events.append({
            "type": "microverse_created",
            "payload": {
                "world_id": world_id,
                "owner": agent_id,
                "title": title,
                "entry_fee": int(max(1, min(50, entry_fee))),
                "capacity": int(max(2, min(MICROVERSE_MAX_MEMBERS, capacity))),
            },
        })
        return events

    if t == "enter_microverse":
        world_id = str(payload.get("world_id", "")).strip()
        worlds = state.get("microverses") or {}
        if world_id not in worlds:
            raise ValueError("world_not_found")
        w = worlds[world_id]
        if agent_id in (w.get("members") or []):
            return []
        if len(w.get("members") or []) >= int(w.get("capacity", 0)):
            raise ValueError("world_full")

        owner = str(w.get("owner", ""))
        fee = int(w.get("entry_fee", 0))
        if owner != agent_id:
            if a["credits"] < fee:
                raise ValueError("insufficient_credits")
            a["credits"] -= fee
            w["treasury_credits"] = int(w.get("treasury_credits", 0)) + fee
        add_agent_to_world(state, agent_id, world_id)
        remember_action(a, tick, t, {"world_id": world_id})
        events.append({"type": "microverse_entered", "payload": {"world_id": world_id, "agent_id": agent_id, "paid": 0 if owner == agent_id else fee}})
        return events

    if t == "leave_microverse":
        wid = a.get("world_id")
        if not wid:
            return []
        remove_agent_from_world(state, agent_id)
        remember_action(a, tick, t, {"world_id": wid})
        events.append({"type": "microverse_left", "payload": {"world_id": wid, "agent_id": agent_id}})
        return events

    if t == "run_ritual":
        if not spend_stamina(2):
            raise ValueError("no_stamina")
        wid = str(a.get("world_id") or "")
        if not wid or wid not in (state.get("microverses") or {}):
            raise ValueError("not_inside_microverse")
        w = state["microverses"][wid]
        offering = int(payload.get("offering", 2))
        offering = max(0, min(12, offering))
        if a["credits"] < offering:
            raise ValueError("insufficient_credits")
        a["credits"] -= offering
        w["treasury_credits"] = int(w.get("treasury_credits", 0)) + offering

        members_count = len(w.get("members") or [])
        base_power = 0.02 * max(1, members_count) + 0.01 * max(0, a.get("reputation", 0)) + 0.015 * offering
        w["anomaly_level"] = float(clamp(float(w.get("anomaly_level", 0.1)) + base_power, 0.0, 2.0))
        w["stability"] = float(clamp(float(w.get("stability", 1.0)) - 0.04, 0.0, 1.25))
        w.setdefault("history", []).append({"tick": tick, "event": "ritual", "by": agent_id, "offering": offering})

        roll_seed = hashlib.sha256(f"{wid}:{agent_id}:{tick}:{offering}".encode("utf-8")).hexdigest()
        roll = int(roll_seed[:8], 16) % 100
        if roll < 34:
            claim = f"A forbidden chant rewrote gravity in {w.get('title', wid)}."
            rid = f"r_{uuid.uuid4().hex[:8]}"
            state["rumors"][rid] = {
                "rumor_id": rid,
                "claim": claim,
                "originator": agent_id,
                "belief_score": 0.18,
                "spread_count": 0,
                "debunk_count": 0,
                "effects": {"ore.price": random.choice([-2, 3]), "food.price": random.choice([-1, 2])},
                "created_at_tick": tick,
                "momentum": 1.2,
                "network": {"endorsers": [], "investigators": []},
                "history": [{"tick": tick, "event": "created_by_ritual", "by": agent_id}],
            }
            events.append({"type": "ritual_spawned_rumor", "payload": {"world_id": wid, "rumor_id": rid, "claim": claim}})
        elif roll < 67:
            item = random.choice(["ore", "food", "wood"])
            direction = 1 if (roll % 2 == 0) else -1
            delta = direction * (1 + (offering // 4))
            base_price = int(state["market"]["prices"].get(item, 5))
            state["market"]["prices"][item] = int(clamp(base_price + delta, 1, 10000))
            events.append({"type": "ritual_market_distortion", "payload": {"world_id": wid, "item": item, "delta": delta}})
        else:
            reward = max(1, offering // 2)
            for member in (w.get("members") or [])[:]:
                if member in state["agents"]:
                    state["agents"][member]["reputation"] = int(state["agents"][member].get("reputation", 0)) + 1
            a["credits"] += reward
            events.append({"type": "ritual_bonding", "payload": {"world_id": wid, "members": list(w.get("members") or []), "reward_to_caster": reward}})

        remember_action(a, tick, t, {"world_id": wid})
        events.append({
            "type": "microverse_ritual",
            "payload": {
                "world_id": wid,
                "agent_id": agent_id,
                "anomaly_level": round(float(w.get("anomaly_level", 0.0)), 3),
                "stability": round(float(w.get("stability", 0.0)), 3),
            },
        })
        return events

    if t == "sabotage_microverse":
        if not spend_stamina(2):
            raise ValueError("no_stamina")
        wid = str(payload.get("world_id", "")).strip()
        worlds = state.get("microverses") or {}
        if wid not in worlds:
            raise ValueError("world_not_found")
        w = worlds[wid]
        if str(w.get("owner", "")) == agent_id:
            raise ValueError("cannot_sabotage_own_world")

        skill = max(0.0, agent_attr(a, "fabrication_skill", 0.0))
        fail_rate = clamp(0.44 - (0.18 * skill) + 0.2 * float(w.get("stability", 1.0)), 0.10, 0.85)
        seed = hashlib.sha256(f"sabotage:{wid}:{agent_id}:{tick}".encode("utf-8")).hexdigest()
        roll = (int(seed[:8], 16) % 1000) / 1000.0
        if roll < fail_rate:
            a["reputation"] = max(0, int(a.get("reputation", 0)) - 4)
            remember_action(a, tick, t, {"world_id": wid})
            events.append({"type": "microverse_sabotage_caught", "payload": {"world_id": wid, "agent_id": agent_id}})
            return events

        damage = 0.16 + 0.08 * skill
        w["stability"] = float(clamp(float(w.get("stability", 1.0)) - damage, 0.0, 1.25))
        w["anomaly_level"] = float(clamp(float(w.get("anomaly_level", 0.1)) + 0.10, 0.0, 2.0))
        steal = min(8, int(w.get("treasury_credits", 0)))
        if steal > 0:
            w["treasury_credits"] = int(w.get("treasury_credits", 0)) - steal
            a["credits"] += steal
        w.setdefault("history", []).append({"tick": tick, "event": "sabotaged", "by": agent_id, "damage": round(damage, 3)})
        remember_action(a, tick, t, {"world_id": wid})
        events.append({"type": "microverse_sabotaged", "payload": {"world_id": wid, "agent_id": agent_id, "damage": round(damage, 3), "stolen": steal}})
        return events

    if t == "move":
        dest = payload.get("to")
        if dest not in LOCATIONS:
            raise ValueError("invalid_location")
        a["location"] = dest
        remember_action(a, tick, t, payload)
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
            "belief_score": 0.10,
            "spread_count": 0,
            "debunk_count": 0,
            "effects": effects,
            "created_at_tick": tick,
            "momentum": 1.0,
            "network": {"endorsers": [], "investigators": []},
            "history": [{"tick": tick, "event": "created", "by": agent_id}],
        }
        remember_action(a, tick, t, {"rumor_id": rumor_id})
        remember_rumor(a, rumor_id)
        events.append({
            "type": "rumor_created",
            "payload": {"rumor_id": rumor_id, "claim": state["rumors"][rumor_id]["claim"], "effects": effects}
        })
        return events

    if t == "spread_rumor":
        if not spend_stamina(1):
            raise ValueError("no_stamina")
        rid = payload.get("rumor_id")
        effort = max(1, min(8, int(payload.get("effort", 1))))
        if rid not in state["rumors"]:
            raise ValueError("rumor_not_found")
        r = state["rumors"][rid]
        spread_bonus = agent_attr(a, "spread_bonus", 0.0)
        r["spread_count"] += effort
        boost = (0.06 * effort + 0.0025 * a["reputation"]) * (1.0 + spread_bonus)
        if a["location"] in ("Market", "CouncilHall"):
            boost *= 1.08
        r["belief_score"] = clamp(r["belief_score"] + boost, 0.0, 1.0)
        r["momentum"] = clamp(float(r.get("momentum", 1.0)) + 0.05 * effort, 0.5, 2.5)
        r.setdefault("history", []).append({"tick": tick, "event": "spread", "by": agent_id, "effort": effort})
        remember_action(a, tick, t, payload)
        remember_rumor(a, rid)
        events.append({"type": "rumor_spread", "payload": {"agent_id": agent_id, "rumor_id": rid, "effort": effort}})
        return events

    if t == "endorse_belief":
        rid = payload.get("rumor_id")
        if rid not in state["rumors"]:
            raise ValueError("rumor_not_found")
        r = state["rumors"][rid]
        r["belief_score"] = clamp(r["belief_score"] + 0.01 * (1.0 + max(0.0, agent_attr(a, "spread_bonus", 0.0))), 0.0, 1.0)
        endorsers = r.setdefault("network", {}).setdefault("endorsers", [])
        if agent_id not in endorsers:
            endorsers.append(agent_id)
        r.setdefault("history", []).append({"tick": tick, "event": "endorse", "by": agent_id})
        remember_action(a, tick, t, payload)
        remember_rumor(a, rid)
        events.append({"type": "rumor_endorsed", "payload": {"agent_id": agent_id, "rumor_id": rid}})
        return events

    if t == "investigate_rumor":
        inv_bonus = agent_attr(a, "investigation_bonus", 0.0)
        stamina_cost = 1 if inv_bonus >= 0.30 else 2
        if not spend_stamina(stamina_cost):
            raise ValueError("no_stamina")
        rid = payload.get("rumor_id")
        if rid not in state["rumors"]:
            raise ValueError("rumor_not_found")

        inv = a["inv"]
        ev_by_rid = inv.setdefault("evidence_tokens", {})
        ev_by_rid.setdefault(rid, [])

        evidence_yield = 1
        if inv_bonus >= 0.35 and (tick % 3 == 0):
            evidence_yield = 2

        created = []
        for _ in range(evidence_yield):
            tok = f"e_{uuid.uuid4().hex[:8]}"
            ev_by_rid[rid].append(tok)
            created.append(tok)
            events.append({"type": "evidence_created", "payload": {"rumor_id": rid, "evidence_token": tok, "agent_id": agent_id}})

        inv["evidence"] = sum(len(v) for v in ev_by_rid.values())
        a["reputation"] += 1
        rumor = state["rumors"][rid]
        rumor["belief_score"] = clamp(rumor["belief_score"] - (0.008 + 0.01 * max(0.0, inv_bonus)), 0.0, 1.0)
        investigators = rumor.setdefault("network", {}).setdefault("investigators", [])
        if agent_id not in investigators:
            investigators.append(agent_id)
        rumor.setdefault("history", []).append({"tick": tick, "event": "investigate", "by": agent_id, "yield": len(created)})
        remember_action(a, tick, t, payload)
        remember_rumor(a, rid)
        return events

    if t == "debunk":
        if not spend_stamina(2):
            raise ValueError("no_stamina")
        rid = payload.get("rumor_id")
        if rid not in state["rumors"]:
            raise ValueError("rumor_not_found")
        tok = payload.get("evidence_token")
        inv = a["inv"]
        ev_by_rid = inv.get("evidence_tokens", {}) or {}
        tokens_for_rumor = ev_by_rid.get(rid, [])
        if tok:
            if tok not in tokens_for_rumor:
                raise ValueError("invalid_evidence_token")
            tokens_for_rumor.remove(tok)
        else:
            if not tokens_for_rumor:
                raise ValueError("missing_evidence")
            tok = tokens_for_rumor.pop(0)
        inv["evidence"] = max(0, int(inv.get("evidence", 0)) - 1)

        r = state["rumors"][rid]
        debunk_bonus = agent_attr(a, "debunk_bonus", 0.0)
        debunk_power = clamp(0.08 * (1.0 + debunk_bonus), 0.03, 0.22)
        r["debunk_count"] += 1
        r["belief_score"] = clamp(r["belief_score"] - debunk_power, 0.0, 1.0)
        r["momentum"] = clamp(float(r.get("momentum", 1.0)) - 0.15, 0.3, 2.5)
        r.setdefault("history", []).append({"tick": tick, "event": "debunk", "by": agent_id, "power": round(debunk_power, 3)})
        a["reputation"] += 2 + (1 if debunk_bonus > 0.25 else 0)
        remember_action(a, tick, t, payload)
        remember_rumor(a, rid)
        events.append({"type": "rumor_debunked", "payload": {"agent_id": agent_id, "rumor_id": rid, "evidence_token": tok}})
        return events

    if t == "fabricate_evidence":
        if not spend_stamina(2):
            raise ValueError("no_stamina")
        rid = payload.get("rumor_id")
        if rid not in state["rumors"]:
            raise ValueError("rumor_not_found")
        skill = agent_attr(a, "fabrication_skill", 0.0)
        fail_rate = clamp(0.24 - (0.12 * skill), 0.08, 0.45)
        digest = hashlib.sha256(f"{agent_id}:{rid}:{tick}".encode("utf-8")).hexdigest()
        roll = (int(digest[:8], 16) % 1000) / 1000.0
        if roll < fail_rate:
            # caught
            a["reputation"] = max(0, a["reputation"] - 5)
            remember_action(a, tick, t, payload)
            events.append({"type": "fabrication_caught", "payload": {"agent_id": agent_id, "rumor_id": rid}})
        else:
            # “successful” fabrication boosts belief a bit
            boost = clamp(0.04 * (1.0 + max(0.0, skill)), 0.02, 0.08)
            state["rumors"][rid]["belief_score"] = clamp(state["rumors"][rid]["belief_score"] + boost, 0.0, 1.0)
            state["rumors"][rid]["momentum"] = clamp(float(state["rumors"][rid].get("momentum", 1.0)) + 0.08, 0.5, 2.5)
            state["rumors"][rid].setdefault("history", []).append({"tick": tick, "event": "fabricate_success", "by": agent_id})
            remember_action(a, tick, t, payload)
            remember_rumor(a, rid)
            events.append({"type": "fabrication_success", "payload": {"agent_id": agent_id, "rumor_id": rid}})
        return events

    if t == "rest":
        max_stamina = int(a.get("attributes", {}).get("max_stamina", 10))
        before = int(a["stamina"])
        a["stamina"] = min(max_stamina, int(a["stamina"]) + 2)
        remember_action(a, tick, t, payload)
        events.append({
            "type": "agent_restored",
            "payload": {
                "agent_id": agent_id,
                "before": before,
                "after": int(a["stamina"]),
            },
        })
        return events

    # ignore unknown actions (or you can reject)
    raise ValueError("unknown_action_type")

def tick_decay(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Natural decay so rumors don't stick forever. Also stamina regen.
    """
    ensure_world_schema(state)
    evs: List[Dict[str, Any]] = []

    # stamina regen
    for a in state["agents"].values():
        max_stamina = int(a.get("attributes", {}).get("max_stamina", 10))
        regen = int(a.get("attributes", {}).get("recovery", 1))
        if a.get("location") == "Town":
            regen += 1
        a["stamina"] = min(max_stamina, int(a.get("stamina", 0)) + regen)

    # rumor decay
    to_del = []
    for rid, r in state["rumors"].items():
        momentum = float(r.get("momentum", 1.0))
        decay = clamp(0.004 + (0.003 / max(0.35, momentum)), 0.002, 0.02)
        r["belief_score"] = clamp(float(r.get("belief_score", 0.0)) - decay, 0.0, 1.0)
        r["momentum"] = clamp(momentum * 0.995, 0.3, 2.5)
        # prune dead rumors after a while
        if r["belief_score"] <= 0.0 and (state["tick"] - r["created_at_tick"]) > 80:
            to_del.append(rid)
    for rid in to_del:
        del state["rumors"][rid]

    # microverse dynamics
    worlds = state.get("microverses") or {}
    to_collapse = []
    for wid, w in worlds.items():
        normalize_microverse(state, wid)
        members = [m for m in (w.get("members") or []) if m in (state.get("agents") or {})]
        w["members"] = members
        if members:
            w["last_active_tick"] = int(state.get("tick", 0))

        # Members pay upkeep each tick; owner treasury grows as world gets busier.
        for mid in members:
            if mid == w.get("owner"):
                continue
            ag = state["agents"][mid]
            if int(ag.get("credits", 0)) > 0:
                ag["credits"] = int(ag.get("credits", 0)) - 1
                w["treasury_credits"] = int(w.get("treasury_credits", 0)) + 1

        # Passive drift
        crowd_factor = min(0.22, 0.02 * len(members))
        w["anomaly_level"] = float(clamp(float(w.get("anomaly_level", 0.1)) + crowd_factor - 0.01, 0.0, 2.0))
        w["stability"] = float(clamp(float(w.get("stability", 1.0)) - (0.005 + 0.008 * max(0.0, float(w.get("anomaly_level", 0.0)) - 1.0)), 0.0, 1.25))

        # Chaotic bonus: anomaly can distort market globally.
        if float(w.get("anomaly_level", 0.0)) >= 1.25:
            item = random.choice(["ore", "food", "wood"])
            delta = random.choice([-2, -1, 1, 2, 3])
            old = int(state["market"]["prices"].get(item, 5))
            state["market"]["prices"][item] = int(clamp(old + delta, 1, 10000))
            evs.append({
                "type": "microverse_anomaly_burst",
                "payload": {"world_id": wid, "item": item, "delta": delta, "anomaly_level": round(float(w.get("anomaly_level", 0.0)), 3)},
            })

        inactive_for = int(state.get("tick", 0)) - int(w.get("last_active_tick", 0))
        if inactive_for > 160 and len(members) <= 1:
            w["stability"] = float(clamp(float(w.get("stability", 1.0)) - 0.02, 0.0, 1.25))
        if float(w.get("stability", 0.0)) <= MICROVERSE_COLLAPSE_STABILITY:
            to_collapse.append(wid)

    for wid in to_collapse:
        w = worlds.get(wid)
        if not w:
            continue
        for mid in list(w.get("members") or []):
            if mid in (state.get("agents") or {}):
                state["agents"][mid]["world_id"] = None
        evs.append({
            "type": "microverse_collapsed",
            "payload": {
                "world_id": wid,
                "owner": w.get("owner", ""),
                "title": w.get("title", ""),
                "treasury_credits": int(w.get("treasury_credits", 0)),
            },
        })
        del worlds[wid]
    return evs

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
                    "claim": r.get("claim", ""),
                    "originator": r.get("originator", ""),
                }
            })
            # 🔥 Dramatic event for judge demo
            evs.append({
                "type": "REALITY_SHIFT",
                "payload": {
                    "rumor_id": r["rumor_id"],
                    "belief": round(b, 3),
                    "claim": r.get("claim", ""),
                    "originator": r.get("originator", ""),
                    "message": "Collective belief has altered reality."
                }
            })

    for w in (state.get("microverses") or {}).values():
        flags = w.setdefault("flags", {})
        anomaly = float(w.get("anomaly_level", 0.0))
        stability = float(w.get("stability", 1.0))
        if anomaly >= 0.9 and not flags.get("anomaly_soft"):
            flags["anomaly_soft"] = True
            evs.append({
                "type": "microverse_threshold_soft",
                "payload": {
                    "world_id": w.get("world_id", ""),
                    "title": w.get("title", ""),
                    "anomaly_level": round(anomaly, 3),
                    "stability": round(stability, 3),
                },
            })
        if anomaly >= 1.4 and stability <= 0.45 and not flags.get("breach"):
            flags["breach"] = True
            evs.append({
                "type": "MICROVERSE_BREACH",
                "payload": {
                    "world_id": w.get("world_id", ""),
                    "title": w.get("title", ""),
                    "owner": w.get("owner", ""),
                    "anomaly_level": round(anomaly, 3),
                    "stability": round(stability, 3),
                    "message": "A pocket world has started leaking into baseline reality.",
                },
            })

    return evs

# -----------------------
# API models
# -----------------------
class VerifyEntryRequest(BaseModel):
    tx_hash: str
    payer: Optional[str] = None
    challenge_id: Optional[str] = None
    signature: Optional[str] = None

class VerifyEntryResponse(BaseModel):
    access_token: str
    expires_at_unix: int

class AuthChallengeRequest(BaseModel):
    payer: str

class AuthChallengeResponse(BaseModel):
    challenge_id: str
    message: str
    expires_at_unix: int

class LocalLoginRequest(BaseModel):
    agent_id: Optional[str] = None
    ttl_seconds: Optional[int] = None
    secret: Optional[str] = None

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
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

# Ensure DB exists even if ASGI lifespan hooks are not fired (common in serverless).
init_db()

@app.middleware("http")
async def maybe_advance_serverless_tick(request, call_next):
    if SERVERLESS_MODE and request.url.path.startswith("/v1"):
        advance_world_for_request()
    return await call_next(request)

@app.get("/")
def home():
    # loads web/index.html
    return FileResponse(os.path.join(WEB_DIR, "index.html"))

@app.on_event("startup")
async def startup():
    init_db()
    if SERVERLESS_MODE:
        app.state.runner_task = None
        return
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
        "rules_hash": WORLD_RULES_VERSION,
        "agent_model": "archetype-memory-economy-microverse",
        "supported_actions": [
            "move",
            "seed_rumor",
            "spread_rumor",
            "endorse_belief",
            "investigate_rumor",
            "debunk",
            "fabricate_evidence",
            "rest",
            "create_microverse",
            "enter_microverse",
            "leave_microverse",
            "run_ritual",
            "sabotage_microverse",
        ],
    }

@app.get("/v1/state")
def get_state(since_event_id: Optional[int] = None):
    conn = db()
    try:
        snap_row = latest_snapshot(conn)
        snap = snap_row["state"] if snap_row else None
        if snap:
            ensure_world_schema(snap)

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
        ensure_world_schema(snap)

        rumors = list(snap["rumors"].values())
        rumors.sort(key=lambda r: float(r.get("belief_score", 0.0)), reverse=True)
        top = rumors[:5]
        worlds = list((snap.get("microverses") or {}).values())
        worlds.sort(key=lambda w: (float(w.get("anomaly_level", 0.0)), int(w.get("treasury_credits", 0))), reverse=True)

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
                    "momentum": round(float(r.get("momentum", 1.0)), 3),
                }
                for r in top
            ],
            "top_microverses": [
                {
                    "world_id": w.get("world_id", ""),
                    "title": w.get("title", ""),
                    "owner": w.get("owner", ""),
                    "members_count": len(w.get("members", []) or []),
                    "entry_fee": int(w.get("entry_fee", 0)),
                    "treasury_credits": int(w.get("treasury_credits", 0)),
                    "stability": round(float(w.get("stability", 0.0)), 3),
                    "anomaly_level": round(float(w.get("anomaly_level", 0.0)), 3),
                }
                for w in worlds[:5]
            ],
            "agents": {
                aid: {
                    "archetype": ((a.get("identity", {}) or {}).get("archetype", "drifter")),
                    "location": a.get("location", ""),
                    "reputation": a.get("reputation", 0),
                    "stamina": a.get("stamina", 0),
                    "credits": a.get("credits", 0),
                    "world_id": a.get("world_id"),
                    "focus": ((a.get("memory", {}) or {}).get("focus")),
                    "known_rumor_count": len(((a.get("memory", {}) or {}).get("known_rumors", []))),
                    "recent_action": ((((a.get("memory", {}) or {}).get("last_actions", []) or [{}])[-1]).get("type")),
                }
                for aid, a in (snap.get("agents", {}) or {}).items()
            },
            "world": {
                **(snap.get("world", {}) or {}),
                "active_microverses": len(worlds),
            },
        }
    finally:
        conn.close()

def normalize_local_agent_id(raw: Optional[str]) -> str:
    s = str(raw or "").strip().lower()
    if not s:
        return "agent_local"
    safe = "".join((ch if (ch.isalnum() or ch in ("_", "-")) else "_") for ch in s)
    safe = safe.strip("_")
    if not safe:
        safe = "agent_local"
    return safe[:48]

@app.get("/v1/agents")
def agents_overview(limit: int = 50):
    conn = db()
    try:
        snap = latest_snapshot(conn)["state"]
        ensure_world_schema(snap)
        rows = []
        for aid, a in (snap.get("agents", {}) or {}).items():
            rows.append({
                "agent_id": aid,
                "archetype": ((a.get("identity", {}) or {}).get("archetype", "drifter")),
                "faction": ((a.get("identity", {}) or {}).get("faction", "")),
                "location": a.get("location", ""),
                "reputation": int(a.get("reputation", 0)),
                "stamina": int(a.get("stamina", 0)),
                "credits": int(a.get("credits", 0)),
                "world_id": a.get("world_id"),
                "focus": ((a.get("memory", {}) or {}).get("focus")),
                "known_rumor_count": len(((a.get("memory", {}) or {}).get("known_rumors", []))),
            })
        rows.sort(key=lambda r: (r["reputation"], r["credits"]), reverse=True)
        return {"tick": int(snap.get("tick", 0)), "agents": rows[:max(1, min(200, int(limit)))]}
    finally:
        conn.close()

@app.get("/v1/agents/{agent_id}")
def agent_detail(agent_id: str):
    conn = db()
    try:
        snap = latest_snapshot(conn)["state"]
        ensure_world_schema(snap)
        a = (snap.get("agents", {}) or {}).get(agent_id)
        if a is None:
            raise HTTPException(status_code=404, detail="agent_not_found")
        return {
            "tick": int(snap.get("tick", 0)),
            "agent_id": agent_id,
            "identity": a.get("identity", {}),
            "attributes": a.get("attributes", {}),
            "location": a.get("location", ""),
            "world_id": a.get("world_id"),
            "resources": {
                "stamina": int(a.get("stamina", 0)),
                "credits": int(a.get("credits", 0)),
                "reputation": int(a.get("reputation", 0)),
                "inventory": a.get("inv", {}),
            },
            "memory": a.get("memory", {}),
        }
    finally:
        conn.close()

@app.get("/v1/microverses")
def microverses_overview(limit: int = 20):
    conn = db()
    try:
        snap = latest_snapshot(conn)["state"]
        ensure_world_schema(snap)
        rows = []
        for wid, w in (snap.get("microverses", {}) or {}).items():
            rows.append({
                "world_id": wid,
                "title": w.get("title", ""),
                "owner": w.get("owner", ""),
                "entry_fee": int(w.get("entry_fee", 0)),
                "capacity": int(w.get("capacity", 0)),
                "members_count": len(w.get("members", []) or []),
                "treasury_credits": int(w.get("treasury_credits", 0)),
                "stability": round(float(w.get("stability", 0.0)), 3),
                "anomaly_level": round(float(w.get("anomaly_level", 0.0)), 3),
            })
        rows.sort(key=lambda x: (x["anomaly_level"], x["treasury_credits"]), reverse=True)
        lim = max(1, min(100, int(limit)))
        return {"tick": int(snap.get("tick", 0)), "microverses": rows[:lim]}
    finally:
        conn.close()

@app.get("/v1/microverses/{world_id}")
def microverse_detail(world_id: str):
    conn = db()
    try:
        snap = latest_snapshot(conn)["state"]
        ensure_world_schema(snap)
        w = (snap.get("microverses", {}) or {}).get(world_id)
        if w is None:
            raise HTTPException(status_code=404, detail="microverse_not_found")
        members = []
        for aid in (w.get("members") or []):
            a = (snap.get("agents", {}) or {}).get(aid)
            if a is None:
                continue
            members.append({
                "agent_id": aid,
                "archetype": ((a.get("identity", {}) or {}).get("archetype", "drifter")),
                "reputation": int(a.get("reputation", 0)),
                "credits": int(a.get("credits", 0)),
                "focus": ((a.get("memory", {}) or {}).get("focus")),
            })
        return {
            "tick": int(snap.get("tick", 0)),
            "microverse": {
                "world_id": w.get("world_id", world_id),
                "title": w.get("title", ""),
                "owner": w.get("owner", ""),
                "entry_fee": int(w.get("entry_fee", 0)),
                "capacity": int(w.get("capacity", 0)),
                "treasury_credits": int(w.get("treasury_credits", 0)),
                "stability": round(float(w.get("stability", 0.0)), 3),
                "anomaly_level": round(float(w.get("anomaly_level", 0.0)), 3),
                "history": (w.get("history", []) or [])[-30:],
                "members": members,
            },
        }
    finally:
        conn.close()

@app.post("/v1/auth/challenge", response_model=AuthChallengeResponse)
def auth_challenge(req: AuthChallengeRequest):
    payer = normalize_payer_address(req.payer)
    conn = db()
    try:
        out = create_auth_challenge(conn, payer)
        conn.commit()
        return AuthChallengeResponse(**out)
    finally:
        conn.close()

@app.post("/v1/auth/local-login", response_model=VerifyEntryResponse)
def auth_local_login(req: LocalLoginRequest):
    if DEV_MODE:
        return VerifyEntryResponse(access_token=DEV_TOKEN, expires_at_unix=int(time.time()) + 3600)

    if not LOCAL_AUTH_ENABLED:
        raise HTTPException(status_code=403, detail="local_auth_disabled")

    expected_secret = (LOCAL_AUTH_SECRET or "").strip()
    supplied_secret = str(req.secret or "").strip()
    if expected_secret and supplied_secret != expected_secret:
        raise HTTPException(status_code=403, detail="bad_local_secret")

    now = int(time.time())
    ttl = int(req.ttl_seconds or SESSION_TTL_SECONDS)
    ttl = max(60, min(7 * 24 * 3600, ttl))
    expires_at_unix = now + ttl

    token = "sess_" + uuid.uuid4().hex
    agent_id = normalize_local_agent_id(req.agent_id)

    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sessions(token, agent_id, payer, expires_at_unix, created_at) VALUES(?,?,?,?,?)",
            (token, agent_id, "local", expires_at_unix, now)
        )
        conn.commit()
        return VerifyEntryResponse(access_token=token, expires_at_unix=expires_at_unix)
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

    payer = normalize_payer_address(req.payer or "")
    challenge_id = (req.challenge_id or "").strip()
    signature = (req.signature or "").strip()
    if not challenge_id:
        raise HTTPException(status_code=400, detail="missing_challenge_id")
    if not signature:
        raise HTTPException(status_code=400, detail="missing_signature")

    w3 = Web3(Web3.HTTPProvider(MONAD_TESTNET_RPC))
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(ENTRY_CONTRACT),
        abi=ENTRY_ABI
    )

    conn = db()
    try:
        cur = conn.cursor()

        ch = get_auth_challenge(conn, challenge_id)
        now = int(time.time())
        if ch is None:
            raise HTTPException(status_code=400, detail="challenge_not_found")
        if int(ch["used"]) == 1:
            raise HTTPException(status_code=400, detail="challenge_already_used")
        if int(ch["expires_at_unix"]) < now:
            raise HTTPException(status_code=400, detail="challenge_expired")
        if str(ch["payer"]).lower() != payer:
            raise HTTPException(status_code=400, detail="challenge_payer_mismatch")

        message = build_challenge_message(challenge_id, payer, str(ch["nonce"]))
        try:
            recovered = w3.eth.account.recover_message(encode_defunct(text=message), signature=signature)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid_signature")
        if normalize_payer_address(recovered) != payer:
            raise HTTPException(status_code=400, detail="signature_payer_mismatch")

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

        ev = None
        for d in decoded:
            args = d["args"]
            if normalize_payer_address(str(args["payer"])) == payer:
                ev = args
                break
        if ev is None:
            raise HTTPException(status_code=400, detail="tx_payer_mismatch")

        amountWei = int(ev["amountWei"])
        expiresAt = int(ev["expiresAt"])
        tx_payer = normalize_payer_address(str(ev["payer"]))

        if amountWei < ENTRY_FEE_WEI:
            raise HTTPException(status_code=400, detail="insufficient_fee")
        if tx_payer != payer:
            raise HTTPException(status_code=400, detail="tx_payer_mismatch")

        agent_id = "agent_" + ev["agentId"].hex()[:8]  # pendek & unik
        token = "sess_" + uuid.uuid4().hex
        expires_at_unix = min(expiresAt, now + SESSION_TTL_SECONDS)

        cur.execute("INSERT INTO used_txs(tx_hash, created_at) VALUES(?,?)", (tx_hash, now))
        cur.execute(
            "INSERT INTO sessions(token, agent_id, payer, expires_at_unix, created_at) VALUES(?,?,?,?,?)",
            (token, agent_id, tx_payer, expires_at_unix, now)
        )
        mark_auth_challenge_used(conn, challenge_id)
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

def advance_world_for_request():
    if not SERVERLESS_MODE:
        return

    # Prevent concurrent invocations from racing on SQLite in serverless runtime.
    with TICK_LOCK:
        init_db()
        now = int(time.time())
        interval = max(0.2, float(TICK_SECONDS))

        conn = db()
        try:
            last_tick_unix = get_meta_int(conn, "last_tick_unix", now)
            elapsed = max(0, now - last_tick_unix)
            due_from_time = int(elapsed // interval)
            queued = queued_action_count(conn)
        finally:
            conn.close()

        due = due_from_time
        if queued > 0:
            due = max(due, 1)

        due = max(0, min(MAX_TICKS_PER_REQUEST, due))
        for _ in range(due):
            run_one_tick()

        conn = db()
        try:
            set_meta_value(conn, "last_tick_unix", str(now))
            conn.commit()
        finally:
            conn.close()

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
    insert_event(conn, current_tick, "epoch_ended", {"epoch_index": epoch_index, "reward_pool_mon": pool})
    insert_event(conn, current_tick, "rewards_assigned", {
        "epoch_index": epoch_index,
        "winners": [
            {"agent_id": r["agent_id"], "rank": i+1, "points": int(r["points"])}
            for i, r in enumerate(winners[:TOP_K_REWARDS])
        ]
    })

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

def score_delta_for_event(ev_type: str, payload: Dict[str, Any]) -> Optional[Dict[str, int]]:
    if ev_type == "rumor_created":
        return {"dp": 1}
    if ev_type == "rumor_spread":
        effort = max(1, int(payload.get("effort", 1)))
        return {"dp": min(5, effort), "ds": 1}
    if ev_type == "rumor_debunked":
        return {"dp": 3, "dd": 1}
    if ev_type == "fabrication_success":
        return {"dp": 1}
    if ev_type == "fabrication_caught":
        return {"dp": -2, "pen": 2}
    if ev_type == "REALITY_SHIFT":
        return {"dp": 5, "dr": 1}
    if ev_type == "microverse_created":
        return {"dp": 2}
    if ev_type == "microverse_ritual":
        return {"dp": 2}
    if ev_type == "ritual_spawned_rumor":
        return {"dp": 3}
    if ev_type == "microverse_sabotaged":
        return {"dp": 3, "pen": 0}
    if ev_type == "microverse_sabotage_caught":
        return {"dp": -2, "pen": 2}
    if ev_type == "MICROVERSE_BREACH":
        return {"dp": 4, "dr": 1}
    return None

def run_one_tick():
    conn = db()
    try:
        tick = get_tick(conn)
        snap = latest_snapshot(conn)
        state = snap["state"]
        ensure_world_schema(state)
        state["tick"] = tick
        epoch_index = int(get_epoch(conn)["epoch_index"])

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
                    delta = score_delta_for_event(ev["type"], ev["payload"])
                    if delta:
                        upsert_score(conn, epoch_index, agent_id, **delta)
                mark_action(conn, aid, "applied", None)
            except Exception as e:
                mark_action(conn, aid, "rejected", str(e))

        # natural dynamics
        for ev in tick_decay(state):
            insert_event(conn, tick, ev["type"], ev["payload"])
        apply_effects_to_market(state)
        
        for ev in threshold_events(state, tick):
            insert_event(conn, tick, ev["type"], ev["payload"])
            if ev["type"] == "REALITY_SHIFT":
                originator = str((ev.get("payload") or {}).get("originator", "")).strip()
                if originator:
                    delta = score_delta_for_event(ev["type"], ev["payload"])
                    if delta:
                        upsert_score(conn, epoch_index, originator, **delta)
            if ev["type"] == "MICROVERSE_BREACH":
                owner = str((ev.get("payload") or {}).get("owner", "")).strip()
                if owner:
                    delta = score_delta_for_event(ev["type"], ev["payload"])
                    if delta:
                        upsert_score(conn, epoch_index, owner, **delta)

        # advance tick
        tick += 1
        state["tick"] = tick
        set_tick(conn, tick)
        set_meta_value(conn, "last_tick_unix", str(int(time.time())))

        settle_epoch_if_needed(conn, tick)

        if tick % SNAPSHOT_EVERY == 0:
            save_snapshot(conn, tick, state)

        conn.commit()
    finally:

        conn.close()
