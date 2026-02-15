import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
IS_VERCEL = (os.getenv("VERCEL", "").lower() in ("1", "true", "yes")) or bool(os.getenv("VERCEL_ENV"))
SERVERLESS_MODE = os.getenv("SERVERLESS_MODE", "1" if IS_VERCEL else "0") == "1"
DB_PATH = os.getenv(
    "ARENA_DB",
    os.path.join(tempfile.gettempdir(), "betrayal_guilds.db") if IS_VERCEL else "betrayal_guilds.db",
)

TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2.0"))
MAX_TICKS_PER_REQUEST = int(os.getenv("MAX_TICKS_PER_REQUEST", "4"))
MAX_ACTIONS_PER_SUBMIT = int(os.getenv("MAX_ACTIONS_PER_SUBMIT", "6"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))

LOCAL_AUTH_ENABLED = os.getenv("LOCAL_AUTH_ENABLED", "1") == "1"
LOCAL_AUTH_SECRET = os.getenv("LOCAL_AUTH_SECRET", "")
ADMIN_RESET_SECRET = os.getenv("ADMIN_RESET_SECRET", "")
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
DEV_TOKEN = os.getenv("DEV_TOKEN", "dev")

GUILDS: Tuple[str, str] = ("alpha", "omega")
ROLE_POOL: Tuple[str, ...] = ("Vanguard", "Warden", "Broker", "Oracle")
SUPPORTED_ACTIONS: Tuple[str, ...] = (
    "strike",
    "guard",
    "farm",
    "transfer",
    "scan",
    "accuse",
    "sabotage",
    "steal_vault",
    "rest",
)
DEFAULT_AGENT_IDS = [
    "agent_alpha_blade",
    "agent_alpha_shield",
    "agent_alpha_broker",
    "agent_omega_blade",
    "agent_omega_shield",
    "agent_omega_broker",
]

TABLE_META = "bg_meta"
TABLE_EVENTS = "bg_events"
TABLE_ACTIONS = "bg_actions"
TABLE_SESSIONS = "bg_sessions"

TICK_LOCK = threading.Lock()


class LocalLoginRequest(BaseModel):
    agent_id: Optional[str] = None
    ttl_seconds: Optional[int] = None
    secret: Optional[str] = None


class LocalLoginResponse(BaseModel):
    access_token: str
    expires_at_unix: int


class SubmitActionsRequest(BaseModel):
    agent_id: str
    tick_submitted: int = 0
    actions: List[Dict[str, Any]] = Field(default_factory=list)


def clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def seeded_int(seed: str, lo: int, hi: int) -> int:
    if hi <= lo:
        return lo
    raw = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return lo + (int(raw[:8], 16) % (hi - lo + 1))


def db() -> sqlite3.Connection:
    dirname = os.path.dirname(DB_PATH)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str):
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {TABLE_META}(key,value) VALUES(?,?) "
        f"ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str) -> str:
    cur = conn.cursor()
    cur.execute(f"SELECT value FROM {TABLE_META} WHERE key=?", (key,))
    row = cur.fetchone()
    return default if row is None else str(row["value"])


def guild_for_agent(agent_id: str) -> str:
    low = (agent_id or "").lower()
    if "alpha" in low:
        return "alpha"
    if "omega" in low:
        return "omega"
    return "alpha" if seeded_int(f"guild:{agent_id}", 0, 1) == 0 else "omega"


def role_for_agent(agent_id: str) -> str:
    idx = seeded_int(f"role:{agent_id}", 0, len(ROLE_POOL) - 1)
    return ROLE_POOL[idx]


def new_agent(agent_id: str) -> Dict[str, Any]:
    return {
        "agent_id": agent_id,
        "guild": guild_for_agent(agent_id),
        "role": role_for_agent(agent_id),
        "alive": True,
        "hp": 16,
        "energy": 8,
        "credits": 10,
        "suspicion": 0.08,
        "trust": 0.52,
        "revealed": False,
        "secret_alignment": "loyal",
        "cooldowns": {"sabotage": 0, "steal": 0},
        "stats": {"damage_done": 0, "vault_stolen": 0, "self_inflicted": 0},
        "last_action": "-",
    }


def assign_traitors(state: Dict[str, Any]):
    match_id = int(state["match"]["id"])
    for guild in GUILDS:
        members = sorted([aid for aid, row in state["agents"].items() if row["guild"] == guild])
        if not members:
            continue
        pick = members[seeded_int(f"traitor:{match_id}:{guild}", 0, len(members) - 1)]
        for aid in members:
            row = state["agents"][aid]
            row["secret_alignment"] = "traitor" if aid == pick else "loyal"
            row["revealed"] = False


def default_state() -> Dict[str, Any]:
    agents = {aid: new_agent(aid) for aid in DEFAULT_AGENT_IDS}
    state = {
        "tick": 0,
        "match": {"id": 1, "round": 1, "max_rounds": 18, "status": "active", "winner": None, "ended_at": None},
        "guilds": {
            "alpha": {"hp": 100, "vault": 40, "score": 0, "wins": 0},
            "omega": {"hp": 100, "vault": 40, "score": 0, "wins": 0},
        },
        "agents": agents,
        "points": {aid: 0 for aid in agents},
    }
    assign_traitors(state)
    return state


def ensure_agent(state: Dict[str, Any], agent_id: str):
    if agent_id in state["agents"]:
        return
    state["agents"][agent_id] = new_agent(agent_id)
    state["points"][agent_id] = 0


def load_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    text = get_meta(conn, "state", "")
    if not text:
        state = default_state()
        set_meta(conn, "state", json.dumps(state))
        return state
    return json.loads(text)


def save_state(conn: sqlite3.Connection, state: Dict[str, Any]):
    set_meta(conn, "state", json.dumps(state))


def init_db():
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(f"CREATE TABLE IF NOT EXISTS {TABLE_META} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE_EVENTS} ("
            "event_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "tick INTEGER NOT NULL,"
            "type TEXT NOT NULL,"
            "payload TEXT NOT NULL,"
            "created_at INTEGER NOT NULL)"
        )
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE_ACTIONS} ("
            "action_id TEXT PRIMARY KEY,"
            "agent_id TEXT NOT NULL,"
            "tick_submitted INTEGER NOT NULL,"
            "payload TEXT NOT NULL,"
            "status TEXT NOT NULL,"
            "error TEXT,"
            "created_at INTEGER NOT NULL)"
        )
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE_SESSIONS} ("
            "token TEXT PRIMARY KEY,"
            "agent_id TEXT NOT NULL,"
            "payer TEXT NOT NULL,"
            "expires_at_unix INTEGER NOT NULL,"
            "created_at INTEGER NOT NULL)"
        )
        if get_meta(conn, "state", "") == "":
            set_meta(conn, "state", json.dumps(default_state()))
        if get_meta(conn, "last_tick_unix", "") == "":
            set_meta(conn, "last_tick_unix", str(int(time.time())))
        conn.commit()
    finally:
        conn.close()


def insert_event(conn: sqlite3.Connection, tick: int, etype: str, payload: Dict[str, Any]):
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {TABLE_EVENTS}(tick,type,payload,created_at) VALUES(?,?,?,?)",
        (int(tick), etype, json.dumps(payload), int(time.time())),
    )


def queue_action(conn: sqlite3.Connection, action_id: str, agent_id: str, tick_submitted: int, payload: Dict[str, Any]):
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {TABLE_ACTIONS}(action_id,agent_id,tick_submitted,payload,status,error,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (action_id, agent_id, int(tick_submitted), json.dumps(payload), "queued", None, int(time.time())),
    )


def reset_world(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {TABLE_EVENTS}")
    cur.execute(f"DELETE FROM {TABLE_ACTIONS}")
    set_meta(conn, "state", json.dumps(default_state()))
    set_meta(conn, "last_tick_unix", str(int(time.time())))


def project_agent(agent_id: str, row: Dict[str, Any], viewer: Optional[str]) -> Dict[str, Any]:
    out = {
        "agent_id": agent_id,
        "guild": row["guild"],
        "role": row["role"],
        "alive": bool(row["alive"]),
        "hp": int(row["hp"]),
        "energy": int(row["energy"]),
        "credits": int(row["credits"]),
        "suspicion": round(float(row["suspicion"]), 3),
        "trust": round(float(row["trust"]), 3),
        "revealed": bool(row["revealed"]),
        "revealed_alignment": row["secret_alignment"] if row["revealed"] else None,
        "last_action": row.get("last_action", "-"),
    }
    if viewer == agent_id:
        out["is_you"] = True
    return out


def private_contract(agent: Dict[str, Any]) -> Dict[str, Any]:
    if agent["secret_alignment"] == "traitor":
        current = int(agent["stats"]["vault_stolen"]) + int(agent["stats"]["self_inflicted"])
        return {
            "text": "cause chaos and siphon value",
            "current": current,
            "target": 25,
            "percent": int(clamp((current / 25) * 100, 0, 100)),
        }
    return {"text": "help your guild win", "current": 0, "target": 1, "percent": 0}


def auth_agent(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="missing_authorization")
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="invalid_authorization")

    token = authorization.split(" ", 1)[1].strip()
    if DEV_MODE:
        if token != DEV_TOKEN:
            raise HTTPException(status_code=403, detail="bad_token")
        return "dev_agent"

    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT agent_id,expires_at_unix FROM {TABLE_SESSIONS} WHERE token=?",
            (token,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=403, detail="invalid_session_token")
        if int(row["expires_at_unix"]) < int(time.time()):
            raise HTTPException(status_code=403, detail="session_expired")
        return str(row["agent_id"])
    finally:
        conn.close()


def soft_auth_agent(authorization: Optional[str]) -> Optional[str]:
    try:
        return auth_agent(authorization)
    except Exception:
        return None


def apply_action(
    state: Dict[str, Any],
    tick: int,
    agent_id: str,
    payload: Dict[str, Any],
    guards: Dict[str, int],
) -> List[Tuple[str, Dict[str, Any]]]:
    ensure_agent(state, agent_id)
    agent = state["agents"][agent_id]
    if state["match"]["status"] != "active":
        raise ValueError("match_not_active")
    if not agent["alive"]:
        raise ValueError("agent_down")

    action_type = str(payload.get("type", "")).strip()
    if not action_type:
        raise ValueError("missing_action_type")

    guild = agent["guild"]
    enemy_guild = "omega" if guild == "alpha" else "alpha"

    def spend(energy_cost: int):
        if agent["energy"] < energy_cost:
            raise ValueError("no_energy")
        agent["energy"] -= energy_cost

    if action_type == "rest":
        agent["energy"] = int(clamp(agent["energy"] + 3, 0, 8))
        agent["last_action"] = "rest"
        return [("agent_rested", {"agent_id": agent_id})]

    if action_type == "farm":
        spend(1)
        gain = seeded_int(f"{tick}:{agent_id}:farm", 2, 5) + (1 if agent["role"] == "Broker" else 0)
        agent["credits"] += gain
        agent["last_action"] = f"farm+{gain}"
        state["points"][agent_id] += 1
        return [("credits_farmed", {"agent_id": agent_id, "gain": gain})]

    if action_type == "guard":
        spend(1)
        target = str(payload.get("target_agent") or agent_id)
        if target not in state["agents"] or state["agents"][target]["guild"] != guild:
            raise ValueError("guard_target_must_be_ally")
        guards[target] = int(guards.get(target, 0)) + 1
        agent["last_action"] = f"guard->{target}"
        state["points"][agent_id] += 2
        return [("agent_guarded", {"agent_id": agent_id, "target_agent": target})]

    if action_type == "transfer":
        target = str(payload.get("target_agent") or "")
        amount = int(payload.get("amount", 0))
        if target not in state["agents"] or state["agents"][target]["guild"] != guild:
            raise ValueError("transfer_target_must_be_ally")
        if amount <= 0 or agent["credits"] < amount:
            raise ValueError("invalid_transfer_amount")
        agent["credits"] -= amount
        state["agents"][target]["credits"] += amount
        agent["last_action"] = f"transfer->{target}:{amount}"
        return [("credits_transferred", {"agent_id": agent_id, "target_agent": target, "amount": amount})]

    if action_type == "scan":
        spend(1)
        target = str(payload.get("target_agent") or "")
        if target not in state["agents"] or state["agents"][target]["guild"] != guild or target == agent_id:
            raise ValueError("scan_target_invalid")
        truth = state["agents"][target]["secret_alignment"] == "traitor"
        accuracy = 0.68 if agent["role"] == "Oracle" else 0.53
        roll = seeded_int(f"{tick}:{agent_id}:{target}:scan", 0, 1000) / 1000.0
        anomaly = (roll < accuracy and truth) or (roll >= accuracy and not truth)
        signal = "anomaly_detected" if anomaly else "clean_reading"
        agent["last_action"] = f"scan->{target}:{signal}"
        state["points"][agent_id] += 1
        return [("scan_result", {"agent_id": agent_id, "target_agent": target, "signal": signal})]

    if action_type == "accuse":
        spend(1)
        target = str(payload.get("target_agent") or "")
        if target not in state["agents"] or state["agents"][target]["guild"] != guild or target == agent_id:
            raise ValueError("accuse_target_invalid")
        hit = state["agents"][target]["secret_alignment"] == "traitor"
        if hit:
            state["agents"][target]["suspicion"] = float(clamp(state["agents"][target]["suspicion"] + 0.35, 0, 1.2))
            state["points"][agent_id] += 3
            agent["last_action"] = f"accuse_hit->{target}"
            return [("accusation_hit", {"agent_id": agent_id, "target_agent": target})]
        agent["suspicion"] = float(clamp(agent["suspicion"] + 0.18, 0, 1.2))
        state["points"][agent_id] -= 1
        agent["last_action"] = f"accuse_miss->{target}"
        return [("accusation_miss", {"agent_id": agent_id, "target_agent": target})]

    if action_type == "sabotage":
        spend(2)
        if int(agent["cooldowns"]["sabotage"]) > 0:
            raise ValueError("sabotage_cooldown")
        dmg = seeded_int(f"{tick}:{agent_id}:sabotage", 3, 8)
        state["guilds"][guild]["hp"] = max(0, int(state["guilds"][guild]["hp"]) - dmg)
        agent["cooldowns"]["sabotage"] = 3
        bump = 0.26 if agent["secret_alignment"] == "traitor" else 0.42
        agent["suspicion"] = float(clamp(agent["suspicion"] + bump, 0, 1.2))
        agent["stats"]["self_inflicted"] += dmg
        agent["last_action"] = f"sabotage-{dmg}"
        state["points"][agent_id] += 2 if agent["secret_alignment"] == "traitor" else -2
        return [("guild_sabotaged", {"agent_id": agent_id, "guild": guild, "damage": dmg})]

    if action_type == "steal_vault":
        spend(2)
        if int(agent["cooldowns"]["steal"]) > 0:
            raise ValueError("steal_cooldown")
        if int(state["guilds"][guild]["vault"]) <= 0:
            raise ValueError("vault_empty")
        amount = min(int(state["guilds"][guild]["vault"]), seeded_int(f"{tick}:{agent_id}:steal", 2, 6))
        state["guilds"][guild]["vault"] -= amount
        agent["credits"] += amount
        agent["cooldowns"]["steal"] = 4
        bump = 0.30 if agent["secret_alignment"] == "traitor" else 0.46
        agent["suspicion"] = float(clamp(agent["suspicion"] + bump, 0, 1.2))
        agent["stats"]["vault_stolen"] += amount
        agent["last_action"] = f"steal+{amount}"
        state["points"][agent_id] += 2 if agent["secret_alignment"] == "traitor" else -2
        return [("vault_stolen", {"agent_id": agent_id, "guild": guild, "amount": amount})]

    if action_type == "strike":
        spend(2)
        target = str(payload.get("target_agent") or "")
        if target not in state["agents"] or state["agents"][target]["guild"] != enemy_guild:
            raise ValueError("strike_target_invalid")
        if not state["agents"][target]["alive"]:
            raise ValueError("strike_target_invalid")
        base = seeded_int(f"{tick}:{agent_id}:{target}:strike", 3, 7) + (1 if agent["role"] == "Vanguard" else 0)
        reduction = 0.63 ** int(guards.get(target, 0))
        damage = max(1, int(round(base * reduction)))
        target_agent = state["agents"][target]
        target_agent["hp"] = max(0, int(target_agent["hp"]) - damage)
        agent["stats"]["damage_done"] += damage
        state["guilds"][enemy_guild]["hp"] = max(0, int(state["guilds"][enemy_guild]["hp"]) - max(1, damage // 2))

        downed = False
        if target_agent["hp"] <= 0 and target_agent["alive"]:
            target_agent["alive"] = False
            downed = True
            state["guilds"][enemy_guild]["hp"] = max(0, int(state["guilds"][enemy_guild]["hp"]) - 5)

        state["points"][agent_id] += 4 if downed else 2
        agent["last_action"] = f"strike->{target}:{damage}"
        events = [("strike_landed", {"agent_id": agent_id, "target_agent": target, "damage": damage, "downed": downed})]
        if downed:
            events.insert(0, ("agent_downed", {"agent_id": target, "by": agent_id}))
        return events

    raise ValueError("invalid_action_type")


def end_match_if_needed(state: Dict[str, Any], tick: int, conn: sqlite3.Connection):
    guilds = state["guilds"]
    match = state["match"]
    ended = (
        int(guilds["alpha"]["hp"]) <= 0
        or int(guilds["omega"]["hp"]) <= 0
        or int(match["round"]) > int(match["max_rounds"])
    )
    if not ended:
        return

    alpha_alive = sum(1 for row in state["agents"].values() if row["guild"] == "alpha" and row["alive"])
    omega_alive = sum(1 for row in state["agents"].values() if row["guild"] == "omega" and row["alive"])
    alpha_score = int(guilds["alpha"]["hp"]) + int(guilds["alpha"]["vault"]) * 2 + (alpha_alive * 6)
    omega_score = int(guilds["omega"]["hp"]) + int(guilds["omega"]["vault"]) * 2 + (omega_alive * 6)
    winner: Optional[str] = None
    if alpha_score != omega_score:
        winner = "alpha" if alpha_score > omega_score else "omega"

    match["status"] = "ended"
    match["winner"] = winner
    match["ended_at"] = tick
    if winner in GUILDS:
        state["guilds"][winner]["wins"] += 1

    for aid, row in state["agents"].items():
        row["revealed"] = True
        state["points"][aid] += 8 if (winner and row["guild"] == winner) else -2

    insert_event(conn, tick, "match_ended", {"match_id": int(match["id"]), "winner": winner})


def restart_match(state: Dict[str, Any], tick: int, conn: sqlite3.Connection):
    state["match"]["id"] = int(state["match"]["id"]) + 1
    state["match"]["round"] = 1
    state["match"]["status"] = "active"
    state["match"]["winner"] = None
    state["match"]["ended_at"] = None

    for guild in GUILDS:
        state["guilds"][guild]["hp"] = 100
        state["guilds"][guild]["vault"] = 40
        state["guilds"][guild]["score"] = 0

    for row in state["agents"].values():
        row["alive"] = True
        row["hp"] = 16
        row["energy"] = 8
        row["suspicion"] = 0.08
        row["trust"] = 0.52
        row["cooldowns"] = {"sabotage": 0, "steal": 0}
        row["stats"] = {"damage_done": 0, "vault_stolen": 0, "self_inflicted": 0}
        row["last_action"] = "-"

    assign_traitors(state)
    insert_event(conn, tick, "match_started", {"match_id": int(state["match"]["id"])})


def run_one_tick():
    conn = db()
    try:
        state = load_state(conn)
        tick = int(state.get("tick", 0))
        cur = conn.cursor()
        cur.execute(
            f"SELECT action_id,agent_id,payload FROM {TABLE_ACTIONS} "
            "WHERE status='queued' AND tick_submitted<=? "
            "ORDER BY created_at ASC, action_id ASC LIMIT 400",
            (tick,),
        )
        queued = cur.fetchall()
        guards: Dict[str, int] = {}

        if state["match"]["status"] == "active":
            for row in queued:
                action_id = str(row["action_id"])
                agent_id = str(row["agent_id"])
                payload = json.loads(row["payload"])
                try:
                    emitted = apply_action(state, tick, agent_id, payload, guards)
                    for etype, epayload in emitted:
                        insert_event(conn, tick, etype, epayload)
                    cur.execute(
                        f"UPDATE {TABLE_ACTIONS} SET status='applied', error=NULL WHERE action_id=?",
                        (action_id,),
                    )
                except Exception as exc:
                    cur.execute(
                        f"UPDATE {TABLE_ACTIONS} SET status='rejected', error=? WHERE action_id=?",
                        (str(exc), action_id),
                    )
                    insert_event(
                        conn,
                        tick,
                        "action_rejected",
                        {"agent_id": agent_id, "action": payload.get("type"), "error": str(exc)},
                    )

            for row in state["agents"].values():
                row["energy"] = int(clamp(row["energy"] + (1 if row["alive"] else 0), 0, 8))
                row["suspicion"] = float(clamp(row["suspicion"] - 0.06, 0, 1.2))
                trust_delta = 0.01 if row["suspicion"] <= 0.2 else -0.01
                row["trust"] = float(clamp(row["trust"] + trust_delta, 0, 1))
                row["cooldowns"]["sabotage"] = max(0, int(row["cooldowns"]["sabotage"]) - 1)
                row["cooldowns"]["steal"] = max(0, int(row["cooldowns"]["steal"]) - 1)
                if row["suspicion"] >= 1.0 and not row["revealed"]:
                    row["revealed"] = True
                    insert_event(
                        conn,
                        tick,
                        "betrayal_revealed",
                        {"agent_id": row["agent_id"], "guild": row["guild"], "alignment": row["secret_alignment"]},
                    )

            for guild in GUILDS:
                state["guilds"][guild]["vault"] = int(state["guilds"][guild]["vault"]) + 1

            state["match"]["round"] = int(state["match"]["round"]) + 1
            end_match_if_needed(state, tick, conn)
        else:
            ended_at = state["match"].get("ended_at")
            if ended_at is not None and (tick - int(ended_at)) >= 2:
                restart_match(state, tick, conn)

        state["tick"] = tick + 1
        save_state(conn, state)
        set_meta(conn, "last_tick_unix", str(int(time.time())))
        conn.commit()
    finally:
        conn.close()


def advance_world_for_request():
    if not SERVERLESS_MODE:
        return
    with TICK_LOCK:
        init_db()
        conn = db()
        try:
            last_tick = int(get_meta(conn, "last_tick_unix", str(int(time.time()))))
            now = int(time.time())
            elapsed = max(0, now - last_tick)
            due = min(MAX_TICKS_PER_REQUEST, int(elapsed // max(0.2, TICK_SECONDS)))
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) AS n FROM {TABLE_ACTIONS} WHERE status='queued'")
            queued_count = int(cur.fetchone()["n"])
        finally:
            conn.close()

        if queued_count > 0:
            due = max(due, 1)
        for _ in range(max(0, due)):
            run_one_tick()


app = FastAPI(title="Betrayal Guilds Arena")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
init_db()


@app.middleware("http")
async def tick_middleware(request, call_next):
    if request.url.path.startswith("/v1"):
        advance_world_for_request()
    return await call_next(request)


@app.get("/")
def home():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/v1/world")
def world():
    return {
        "title": "Betrayal Guilds Arena",
        "mode": "gaming-agents",
        "engine": "betrayal-guilds-v2",
        "tick_seconds": TICK_SECONDS,
        "supported_actions": list(SUPPORTED_ACTIONS),
    }


@app.get("/v1/state")
def state(since_event_id: Optional[int] = None, authorization: Optional[str] = Header(default=None)):
    viewer = soft_auth_agent(authorization)
    conn = db()
    try:
        s = load_state(conn)
        snapshot = {
            "tick": int(s["tick"]),
            "match": s["match"],
            "guilds": s["guilds"],
            "agents": {aid: project_agent(aid, row, viewer) for aid, row in s["agents"].items()},
        }

        cur = conn.cursor()
        if since_event_id is None:
            cur.execute(f"SELECT COALESCE(MAX(event_id),0) AS m FROM {TABLE_EVENTS}")
            latest = int(cur.fetchone()["m"])
            return {"snapshot": snapshot, "events": [], "latest_event_id": latest}

        cur.execute(
            f"SELECT event_id,tick,type,payload FROM {TABLE_EVENTS} "
            "WHERE event_id>? ORDER BY event_id ASC LIMIT 500",
            (int(since_event_id),),
        )
        rows = cur.fetchall()
        events = [
            {
                "event_id": int(row["event_id"]),
                "tick": int(row["tick"]),
                "type": str(row["type"]),
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]
        latest = events[-1]["event_id"] if events else int(since_event_id)
        return {"snapshot": snapshot, "events": events, "latest_event_id": latest}
    finally:
        conn.close()


@app.get("/v1/summary")
def summary(authorization: Optional[str] = Header(default=None)):
    viewer = soft_auth_agent(authorization)
    conn = db()
    try:
        s = load_state(conn)
        agents = {aid: project_agent(aid, row, viewer) for aid, row in s["agents"].items()}
        suspects = sorted(agents.values(), key=lambda row: row["suspicion"], reverse=True)[:6]
        leaders = sorted(
            [{"agent_id": aid, "points": int(points)} for aid, points in s["points"].items()],
            key=lambda row: row["points"],
            reverse=True,
        )[:10]

        out = {
            "tick": int(s["tick"]),
            "match": s["match"],
            "guilds": s["guilds"],
            "agents": agents,
            "top_suspects": suspects,
            "leaderboard": leaders,
        }
        if viewer and viewer in s["agents"]:
            me = s["agents"][viewer]
            out["self"] = {
                "agent_id": viewer,
                "guild": me["guild"],
                "role": me["role"],
                "alive": me["alive"],
                "hp": me["hp"],
                "energy": me["energy"],
                "credits": me["credits"],
                "suspicion": round(float(me["suspicion"]), 3),
                "trust": round(float(me["trust"]), 3),
                "secret_alignment": me["secret_alignment"],
                "contract": private_contract(me),
            }
        return out
    finally:
        conn.close()


@app.post("/v1/actions")
def submit_actions(req: SubmitActionsRequest, authorization: Optional[str] = Header(default=None)):
    authed = auth_agent(authorization)
    if not DEV_MODE and req.agent_id != authed:
        raise HTTPException(status_code=403, detail="agent_id_mismatch")
    if len(req.actions) > MAX_ACTIONS_PER_SUBMIT:
        raise HTTPException(status_code=400, detail=f"too_many_actions_max_{MAX_ACTIONS_PER_SUBMIT}")

    conn = db()
    try:
        s = load_state(conn)
        ensure_agent(s, req.agent_id)
        save_state(conn, s)
        ids: List[str] = []
        for payload in req.actions:
            action_id = str(uuid.uuid4())
            queue_action(conn, action_id, req.agent_id, int(req.tick_submitted), payload)
            ids.append(action_id)
        conn.commit()
        return {"accepted": True, "action_ids": ids}
    finally:
        conn.close()


@app.get("/v1/agents")
def agents(limit: int = 100, authorization: Optional[str] = Header(default=None)):
    viewer = soft_auth_agent(authorization)
    conn = db()
    try:
        s = load_state(conn)
        rows = [project_agent(aid, row, viewer) for aid, row in s["agents"].items()]
        rows.sort(key=lambda row: (row["guild"], -row["suspicion"], row["agent_id"]))
        return {"tick": int(s["tick"]), "agents": rows[: max(1, min(300, int(limit)))]}
    finally:
        conn.close()


@app.get("/v1/leaderboard")
def leaderboard(limit: int = 12):
    conn = db()
    try:
        s = load_state(conn)
        leaders = sorted(
            [{"agent_id": aid, "points": int(points)} for aid, points in s["points"].items()],
            key=lambda row: row["points"],
            reverse=True,
        )
        return {"tick": int(s["tick"]), "leaders": leaders[: max(1, min(100, int(limit)))]}
    finally:
        conn.close()


@app.post("/v1/auth/local-login", response_model=LocalLoginResponse)
def local_login(req: LocalLoginRequest):
    if DEV_MODE:
        return LocalLoginResponse(access_token=DEV_TOKEN, expires_at_unix=int(time.time()) + 3600)
    if not LOCAL_AUTH_ENABLED:
        raise HTTPException(status_code=403, detail="local_auth_disabled")

    expected = (LOCAL_AUTH_SECRET or "").strip()
    incoming = str(req.secret or "").strip()
    if expected and incoming != expected:
        raise HTTPException(status_code=403, detail="bad_local_secret")

    agent_id = (str(req.agent_id or "agent_local").strip().lower() or "agent_local")[:48]
    ttl = max(60, min(7 * 24 * 3600, int(req.ttl_seconds or SESSION_TTL_SECONDS)))
    now = int(time.time())
    token = "sess_" + uuid.uuid4().hex

    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO {TABLE_SESSIONS}(token,agent_id,payer,expires_at_unix,created_at) "
            "VALUES(?,?,?,?,?)",
            (token, agent_id, "local", now + ttl, now),
        )
        s = load_state(conn)
        ensure_agent(s, agent_id)
        save_state(conn, s)
        conn.commit()
    finally:
        conn.close()

    return LocalLoginResponse(access_token=token, expires_at_unix=now + ttl)


@app.get("/v1/auth/whoami")
def whoami(authorization: Optional[str] = Header(default=None)):
    agent_id = auth_agent(authorization)
    if DEV_MODE:
        return {"mode": "dev", "agent_id": agent_id, "token_prefix": DEV_TOKEN[:12] + "..."}

    token = authorization.split(" ", 1)[1].strip()
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT agent_id,payer,expires_at_unix,created_at FROM {TABLE_SESSIONS} WHERE token=?",
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


@app.post("/v1/admin/reset-world")
def admin_reset_world(x_admin_secret: Optional[str] = Header(default=None)):
    expected = (ADMIN_RESET_SECRET or "").strip()
    if expected and (x_admin_secret or "").strip() != expected:
        raise HTTPException(status_code=403, detail="bad_admin_secret")

    conn = db()
    try:
        reset_world(conn)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "reset_at_unix": int(time.time())}


async def tick_runner():
    while True:
        try:
            run_one_tick()
        except Exception as exc:
            print("tick_error", repr(exc))
        await asyncio.sleep(TICK_SECONDS)


@app.on_event("startup")
async def startup():
    init_db()
    if SERVERLESS_MODE:
        app.state.runner_task = None
    else:
        app.state.runner_task = asyncio.create_task(tick_runner())


@app.on_event("shutdown")
async def shutdown():
    task = getattr(app.state, "runner_task", None)
    if task:
        task.cancel()
