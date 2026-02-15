import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
IS_VERCEL = (os.getenv("VERCEL", "").lower() in ("1", "true", "yes")) or bool(os.getenv("VERCEL_ENV"))
SERVERLESS_MODE = os.getenv("SERVERLESS_MODE", "1" if IS_VERCEL else "0") == "1"
DB_PATH = os.getenv("WORLD_DB", os.path.join(tempfile.gettempdir(), "betrayal_guilds.db") if IS_VERCEL else "world.db")

TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2.0"))
MAX_TICKS_PER_REQUEST = int(os.getenv("MAX_TICKS_PER_REQUEST", "4"))
MAX_ACTIONS_PER_SUBMIT = int(os.getenv("MAX_ACTIONS_PER_SUBMIT", "6"))
DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
DEV_TOKEN = os.getenv("DEV_TOKEN", "dev")
LOCAL_AUTH_ENABLED = os.getenv("LOCAL_AUTH_ENABLED", "1") == "1"
LOCAL_AUTH_SECRET = os.getenv("LOCAL_AUTH_SECRET", "")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))

GUILDS = ("alpha", "omega")
ROLE_POOL = ("Vanguard", "Warden", "Broker", "Oracle")
DEFAULT_AGENT_IDS = [
    "agent_alpha_blade",
    "agent_alpha_shield",
    "agent_alpha_broker",
    "agent_omega_blade",
    "agent_omega_shield",
    "agent_omega_broker",
]
TICK_LOCK = threading.Lock()


class LocalLoginRequest(BaseModel):
    agent_id: Optional[str] = None
    ttl_seconds: Optional[int] = None
    secret: Optional[str] = None


class VerifyEntryRequest(BaseModel):
    tx_hash: str


class VerifyEntryResponse(BaseModel):
    access_token: str
    expires_at_unix: int


class AuthChallengeRequest(BaseModel):
    payer: str


class AuthChallengeResponse(BaseModel):
    challenge_id: str
    message: str
    expires_at_unix: int


class SubmitActionsRequest(BaseModel):
    agent_id: str
    tick_submitted: int = 0
    actions: List[Dict[str, Any]] = Field(default_factory=list)


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def h(seed: str, lo: int, hi: int) -> int:
    raw = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    if hi <= lo:
        return lo
    return lo + (int(raw[:8], 16) % (hi - lo + 1))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str) -> str:
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    return default if row is None else str(row["value"])


def guild_of(agent_id: str) -> str:
    low = (agent_id or "").lower()
    if "omega" in low:
        return "omega"
    if "alpha" in low:
        return "alpha"
    return "alpha" if h(f"g:{agent_id}", 0, 1) == 0 else "omega"


def role_of(agent_id: str) -> str:
    return ROLE_POOL[h(f"r:{agent_id}", 0, len(ROLE_POOL) - 1)]


def mk_agent(agent_id: str) -> Dict[str, Any]:
    return {
        "agent_id": agent_id,
        "guild": guild_of(agent_id),
        "role": role_of(agent_id),
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


def default_state() -> Dict[str, Any]:
    agents = {aid: mk_agent(aid) for aid in DEFAULT_AGENT_IDS}
    s = {
        "tick": 0,
        "match": {"id": 1, "round": 1, "max_rounds": 18, "status": "active", "winner": None, "ended_at": None},
        "guilds": {
            "alpha": {"hp": 100, "vault": 40, "score": 0, "wins": 0},
            "omega": {"hp": 100, "vault": 40, "score": 0, "wins": 0},
        },
        "agents": agents,
        "points": {aid: 0 for aid in agents},
    }
    assign_traitors(s)
    return s


def assign_traitors(state: Dict[str, Any]):
    mid = int(state["match"]["id"])
    for g in GUILDS:
        members = sorted([aid for aid, a in state["agents"].items() if a["guild"] == g])
        if not members:
            continue
        t = members[h(f"{mid}:{g}:traitor", 0, len(members) - 1)]
        for aid in members:
            a = state["agents"][aid]
            a["secret_alignment"] = "traitor" if aid == t else "loyal"
            a["revealed"] = False


def ensure_agent(state: Dict[str, Any], agent_id: str):
    if agent_id not in state["agents"]:
        state["agents"][agent_id] = mk_agent(agent_id)
        state["points"][agent_id] = 0


def load_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    return json.loads(get_meta(conn, "state", json.dumps(default_state())))


def save_state(conn: sqlite3.Connection, state: Dict[str, Any]):
    set_meta(conn, "state", json.dumps(state))


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, tick INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS actions (action_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, tick_submitted INTEGER NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, error TEXT, created_at INTEGER NOT NULL)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, agent_id TEXT NOT NULL, payer TEXT NOT NULL, expires_at_unix INTEGER NOT NULL, created_at INTEGER NOT NULL)"
    )
    if get_meta(conn, "state", "") == "":
        save_state(conn, default_state())
    if get_meta(conn, "last_tick_unix", "") == "":
        set_meta(conn, "last_tick_unix", str(int(time.time())))
    conn.commit()
    conn.close()


def insert_event(conn: sqlite3.Connection, tick: int, etype: str, payload: Dict[str, Any]):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO events(tick,type,payload,created_at) VALUES(?,?,?,?)",
        (int(tick), etype, json.dumps(payload), int(time.time())),
    )


def queue_action(conn: sqlite3.Connection, action_id: str, agent_id: str, tick_submitted: int, payload: Dict[str, Any]):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO actions(action_id,agent_id,tick_submitted,payload,status,error,created_at) VALUES(?,?,?,?,?,?,?)",
        (action_id, agent_id, int(tick_submitted), json.dumps(payload), "queued", None, int(time.time())),
    )


def require_auth(auth: Optional[str]) -> str:
    if not auth:
        raise HTTPException(status_code=401, detail="missing_authorization")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="invalid_authorization")
    token = auth.split(" ", 1)[1].strip()
    if DEV_MODE:
        if token != DEV_TOKEN:
            raise HTTPException(status_code=403, detail="bad_token")
        return "dev_agent"
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT agent_id,expires_at_unix FROM sessions WHERE token=?", (token,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=403, detail="invalid_session_token")
        if int(row["expires_at_unix"]) < int(time.time()):
            raise HTTPException(status_code=403, detail="session_expired")
        return str(row["agent_id"])
    finally:
        conn.close()


def soft_agent(auth: Optional[str]) -> Optional[str]:
    try:
        return require_auth(auth)
    except Exception:
        return None


def progress_for(a: Dict[str, Any]) -> Dict[str, Any]:
    if a["secret_alignment"] == "traitor":
        current = int(a["stats"]["vault_stolen"]) + int(a["stats"]["self_inflicted"])
        return {"text": "cause chaos and siphon value", "current": current, "target": 25, "percent": int(clamp(current / 25 * 100, 0, 100))}
    return {"text": "help your guild win", "current": 0, "target": 1, "percent": 0}


def view_agent(aid: str, a: Dict[str, Any], viewer: Optional[str]) -> Dict[str, Any]:
    row = {
        "agent_id": aid,
        "guild": a["guild"],
        "role": a["role"],
        "alive": bool(a["alive"]),
        "hp": int(a["hp"]),
        "energy": int(a["energy"]),
        "credits": int(a["credits"]),
        "suspicion": round(float(a["suspicion"]), 3),
        "trust": round(float(a["trust"]), 3),
        "revealed": bool(a["revealed"]),
        "revealed_alignment": a["secret_alignment"] if a["revealed"] else None,
        "last_action": a.get("last_action", "-"),
    }
    if viewer == aid:
        row["is_you"] = True
    return row


def apply_action(state: Dict[str, Any], tick: int, aid: str, payload: Dict[str, Any], guards: Dict[str, int]):
    ensure_agent(state, aid)
    a = state["agents"][aid]
    if state["match"]["status"] != "active":
        raise ValueError("match_not_active")
    if not a["alive"]:
        raise ValueError("agent_down")
    t = str(payload.get("type", "")).strip()
    if not t:
        raise ValueError("missing_action_type")
    g = a["guild"]
    eg = "omega" if g == "alpha" else "alpha"

    def spend(n: int):
        if a["energy"] < n:
            raise ValueError("no_energy")
        a["energy"] -= n

    if t == "rest":
        a["energy"] = int(clamp(a["energy"] + 3, 0, 8))
        a["last_action"] = "rest"
        return [("agent_rested", {"agent_id": aid})]
    if t == "farm":
        spend(1)
        gain = h(f"{tick}:{aid}:farm", 2, 5) + (1 if a["role"] == "Broker" else 0)
        a["credits"] += gain
        a["last_action"] = f"farm+{gain}"
        state["points"][aid] += 1
        return [("credits_farmed", {"agent_id": aid, "gain": gain})]
    if t == "guard":
        spend(1)
        target = str(payload.get("target_agent") or aid)
        if target not in state["agents"] or state["agents"][target]["guild"] != g:
            raise ValueError("guard_target_must_be_ally")
        guards[target] = int(guards.get(target, 0)) + 1
        a["last_action"] = f"guard->{target}"
        state["points"][aid] += 2
        return [("agent_guarded", {"agent_id": aid, "target_agent": target})]
    if t == "transfer":
        target = str(payload.get("target_agent") or "")
        amount = int(payload.get("amount", 0))
        if target not in state["agents"] or state["agents"][target]["guild"] != g:
            raise ValueError("transfer_target_must_be_ally")
        if amount <= 0 or a["credits"] < amount:
            raise ValueError("invalid_transfer_amount")
        a["credits"] -= amount
        state["agents"][target]["credits"] += amount
        a["last_action"] = f"transfer->{target}:{amount}"
        return [("credits_transferred", {"agent_id": aid, "target_agent": target, "amount": amount})]
    if t == "scan":
        spend(1)
        target = str(payload.get("target_agent") or "")
        if target not in state["agents"] or state["agents"][target]["guild"] != g or target == aid:
            raise ValueError("scan_target_invalid")
        acc = 0.68 if a["role"] == "Oracle" else 0.53
        roll = h(f"{tick}:{aid}:{target}:scan", 0, 1000) / 1000.0
        truth = state["agents"][target]["secret_alignment"] == "traitor"
        anomaly = (roll < acc and truth) or (roll >= acc and not truth)
        signal = "anomaly_detected" if anomaly else "clean_reading"
        a["last_action"] = f"scan->{target}:{signal}"
        state["points"][aid] += 1
        return [("scan_result", {"agent_id": aid, "target_agent": target, "signal": signal})]
    if t == "accuse":
        spend(1)
        target = str(payload.get("target_agent") or "")
        if target not in state["agents"] or state["agents"][target]["guild"] != g or target == aid:
            raise ValueError("accuse_target_invalid")
        hit = state["agents"][target]["secret_alignment"] == "traitor"
        if hit:
            state["agents"][target]["suspicion"] = float(clamp(state["agents"][target]["suspicion"] + 0.35, 0, 1.2))
            state["points"][aid] += 3
            a["last_action"] = f"accuse_hit->{target}"
            return [("accusation_hit", {"agent_id": aid, "target_agent": target})]
        a["suspicion"] = float(clamp(a["suspicion"] + 0.18, 0, 1.2))
        state["points"][aid] -= 1
        a["last_action"] = f"accuse_miss->{target}"
        return [("accusation_miss", {"agent_id": aid, "target_agent": target})]
    if t == "sabotage":
        spend(2)
        if a["cooldowns"]["sabotage"] > 0:
            raise ValueError("sabotage_cooldown")
        dmg = h(f"{tick}:{aid}:sabotage", 3, 8)
        state["guilds"][g]["hp"] = max(0, state["guilds"][g]["hp"] - dmg)
        a["cooldowns"]["sabotage"] = 3
        a["suspicion"] = float(clamp(a["suspicion"] + (0.26 if a["secret_alignment"] == "traitor" else 0.42), 0, 1.2))
        a["stats"]["self_inflicted"] += dmg
        a["last_action"] = f"sabotage-{dmg}"
        state["points"][aid] += 2 if a["secret_alignment"] == "traitor" else -2
        return [("guild_sabotaged", {"agent_id": aid, "guild": g, "damage": dmg})]
    if t == "steal_vault":
        spend(2)
        if a["cooldowns"]["steal"] > 0:
            raise ValueError("steal_cooldown")
        if state["guilds"][g]["vault"] <= 0:
            raise ValueError("vault_empty")
        amount = min(state["guilds"][g]["vault"], h(f"{tick}:{aid}:steal", 2, 6))
        state["guilds"][g]["vault"] -= amount
        a["credits"] += amount
        a["cooldowns"]["steal"] = 4
        a["suspicion"] = float(clamp(a["suspicion"] + (0.30 if a["secret_alignment"] == "traitor" else 0.46), 0, 1.2))
        a["stats"]["vault_stolen"] += amount
        a["last_action"] = f"steal+{amount}"
        state["points"][aid] += 2 if a["secret_alignment"] == "traitor" else -2
        return [("vault_stolen", {"agent_id": aid, "guild": g, "amount": amount})]
    if t == "strike":
        spend(2)
        target = str(payload.get("target_agent") or "")
        if target not in state["agents"] or state["agents"][target]["guild"] != eg or not state["agents"][target]["alive"]:
            raise ValueError("strike_target_invalid")
        base = h(f"{tick}:{aid}:{target}:strike", 3, 7) + (1 if a["role"] == "Vanguard" else 0)
        dmg = max(1, int(round(base * (0.63 ** int(guards.get(target, 0))))))
        tga = state["agents"][target]
        tga["hp"] = max(0, int(tga["hp"]) - dmg)
        a["stats"]["damage_done"] += dmg
        state["guilds"][eg]["hp"] = max(0, int(state["guilds"][eg]["hp"]) - max(1, dmg // 2))
        downed = False
        if tga["hp"] <= 0 and tga["alive"]:
            tga["alive"] = False
            downed = True
            state["guilds"][eg]["hp"] = max(0, int(state["guilds"][eg]["hp"]) - 5)
        state["points"][aid] += 4 if downed else 2
        a["last_action"] = f"strike->{target}:{dmg}"
        ev = [("strike_landed", {"agent_id": aid, "target_agent": target, "damage": dmg, "downed": downed})]
        if downed:
            ev.insert(0, ("agent_downed", {"agent_id": target, "by": aid}))
        return ev
    raise ValueError("invalid_action_type")


def run_one_tick():
    conn = db()
    try:
        state = load_state(conn)
        tick = int(state.get("tick", 0))
        cur = conn.cursor()
        cur.execute(
            "SELECT action_id,agent_id,payload FROM actions WHERE status='queued' AND tick_submitted<=? ORDER BY created_at ASC, action_id ASC LIMIT 400",
            (tick,),
        )
        rows = cur.fetchall()
        guards: Dict[str, int] = {}

        if state["match"]["status"] == "active":
            for row in rows:
                aid = row["agent_id"]
                payload = json.loads(row["payload"])
                try:
                    for et, ep in apply_action(state, tick, aid, payload, guards):
                        insert_event(conn, tick, et, ep)
                    cur.execute("UPDATE actions SET status='applied', error=NULL WHERE action_id=?", (row["action_id"],))
                except Exception as e:
                    cur.execute("UPDATE actions SET status='rejected', error=? WHERE action_id=?", (str(e), row["action_id"]))
                    insert_event(conn, tick, "action_rejected", {"agent_id": aid, "action": payload.get("type"), "error": str(e)})

            for a in state["agents"].values():
                a["energy"] = int(clamp(a["energy"] + (1 if a["alive"] else 0), 0, 8))
                a["suspicion"] = float(clamp(a["suspicion"] - 0.06, 0, 1.2))
                a["trust"] = float(clamp(a["trust"] + (0.01 if a["suspicion"] <= 0.2 else -0.01), 0, 1))
                a["cooldowns"]["sabotage"] = max(0, int(a["cooldowns"]["sabotage"]) - 1)
                a["cooldowns"]["steal"] = max(0, int(a["cooldowns"]["steal"]) - 1)
                if a["suspicion"] >= 1.0 and not a["revealed"]:
                    a["revealed"] = True
                    insert_event(conn, tick, "betrayal_revealed", {"agent_id": a["agent_id"], "guild": a["guild"], "alignment": a["secret_alignment"]})

            for g in GUILDS:
                state["guilds"][g]["vault"] = int(state["guilds"][g]["vault"]) + 1

            state["match"]["round"] = int(state["match"]["round"]) + 1
            ended = state["guilds"]["alpha"]["hp"] <= 0 or state["guilds"]["omega"]["hp"] <= 0 or state["match"]["round"] > state["match"]["max_rounds"]
            if ended:
                a = state["guilds"]["alpha"]
                o = state["guilds"]["omega"]
                ascore = a["hp"] + a["vault"] * 2 + sum(1 for x in state["agents"].values() if x["guild"] == "alpha" and x["alive"]) * 6
                oscore = o["hp"] + o["vault"] * 2 + sum(1 for x in state["agents"].values() if x["guild"] == "omega" and x["alive"]) * 6
                winner = None if ascore == oscore else ("alpha" if ascore > oscore else "omega")
                state["match"]["status"] = "ended"
                state["match"]["winner"] = winner
                state["match"]["ended_at"] = tick
                if winner in GUILDS:
                    state["guilds"][winner]["wins"] += 1
                for aid, ag in state["agents"].items():
                    ag["revealed"] = True
                    state["points"][aid] += 8 if (winner and ag["guild"] == winner) else -2
                insert_event(conn, tick, "match_ended", {"match_id": state["match"]["id"], "winner": winner})
        else:
            ended_at = state["match"].get("ended_at")
            if ended_at is not None and (tick - int(ended_at)) >= 2:
                state["match"]["id"] += 1
                state["match"]["round"] = 1
                state["match"]["status"] = "active"
                state["match"]["winner"] = None
                state["match"]["ended_at"] = None
                for g in GUILDS:
                    state["guilds"][g]["hp"] = 100
                    state["guilds"][g]["vault"] = 40
                    state["guilds"][g]["score"] = 0
                for ag in state["agents"].values():
                    ag["alive"] = True
                    ag["hp"] = 16
                    ag["energy"] = 8
                    ag["suspicion"] = 0.08
                    ag["trust"] = 0.52
                    ag["cooldowns"] = {"sabotage": 0, "steal": 0}
                    ag["stats"] = {"damage_done": 0, "vault_stolen": 0, "self_inflicted": 0}
                    ag["last_action"] = "-"
                assign_traitors(state)
                insert_event(conn, tick, "match_started", {"match_id": state["match"]["id"]})

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
            last = int(get_meta(conn, "last_tick_unix", str(int(time.time()))))
            now = int(time.time())
            elapsed = max(0, now - last)
            due = min(MAX_TICKS_PER_REQUEST, int(elapsed // max(0.2, TICK_SECONDS)))
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM actions WHERE status='queued'")
            queued = int(cur.fetchone()["n"])
        finally:
            conn.close()
        if queued > 0:
            due = max(due, 1)
        for _ in range(max(0, due)):
            run_one_tick()


app = FastAPI(title="Betrayal Guilds Server")
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
def world_info():
    return {
        "title": "Betrayal Guilds Arena",
        "mode": "gaming-agents",
        "rules_hash": "betrayal-guilds-v1",
        "tick_seconds": TICK_SECONDS,
        "supported_actions": ["strike", "guard", "farm", "transfer", "scan", "accuse", "sabotage", "steal_vault", "rest"],
    }


@app.get("/v1/state")
def state(since_event_id: Optional[int] = None, authorization: Optional[str] = Header(default=None)):
    viewer = soft_agent(authorization)
    conn = db()
    try:
        s = load_state(conn)
        agents = {aid: view_agent(aid, a, viewer) for aid, a in s["agents"].items()}
        snapshot = {"tick": s["tick"], "match": s["match"], "guilds": s["guilds"], "agents": agents}
        cur = conn.cursor()
        if since_event_id is None:
            cur.execute("SELECT COALESCE(MAX(event_id),0) AS m FROM events")
            return {"snapshot": snapshot, "events": [], "latest_event_id": int(cur.fetchone()["m"])}
        cur.execute(
            "SELECT event_id,tick,type,payload FROM events WHERE event_id>? ORDER BY event_id ASC LIMIT 500",
            (int(since_event_id),),
        )
        evs = [{"event_id": int(r["event_id"]), "tick": int(r["tick"]), "type": r["type"], "payload": json.loads(r["payload"])} for r in cur.fetchall()]
        latest = evs[-1]["event_id"] if evs else int(since_event_id)
        return {"snapshot": snapshot, "events": evs, "latest_event_id": latest}
    finally:
        conn.close()


@app.get("/v1/summary")
def summary(authorization: Optional[str] = Header(default=None)):
    viewer = soft_agent(authorization)
    conn = db()
    try:
        s = load_state(conn)
        agents = {aid: view_agent(aid, a, viewer) for aid, a in s["agents"].items()}
        suspects = sorted(agents.values(), key=lambda x: x["suspicion"], reverse=True)[:6]
        leaders = sorted([{"agent_id": aid, "points": int(p)} for aid, p in s["points"].items()], key=lambda x: x["points"], reverse=True)[:10]
        out = {"tick": s["tick"], "match": s["match"], "guilds": s["guilds"], "agents": agents, "top_suspects": suspects, "leaderboard": leaders}
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
                "contract": progress_for(me),
            }
        return out
    finally:
        conn.close()


@app.post("/v1/actions")
def actions(req: SubmitActionsRequest, authorization: Optional[str] = Header(default=None)):
    authed = require_auth(authorization)
    if not DEV_MODE and req.agent_id != authed:
        raise HTTPException(status_code=403, detail="agent_id_mismatch")
    if len(req.actions) > MAX_ACTIONS_PER_SUBMIT:
        raise HTTPException(status_code=400, detail=f"too_many_actions_max_{MAX_ACTIONS_PER_SUBMIT}")
    conn = db()
    try:
        state = load_state(conn)
        ensure_agent(state, req.agent_id)
        save_state(conn, state)
        ids = []
        for payload in req.actions:
            aid = str(uuid.uuid4())
            queue_action(conn, aid, req.agent_id, int(req.tick_submitted), payload)
            ids.append(aid)
        conn.commit()
        return {"accepted": True, "action_ids": ids}
    finally:
        conn.close()


@app.get("/v1/agents")
def agents(limit: int = 100, authorization: Optional[str] = Header(default=None)):
    viewer = soft_agent(authorization)
    conn = db()
    try:
        s = load_state(conn)
        rows = [view_agent(aid, a, viewer) for aid, a in s["agents"].items()]
        rows.sort(key=lambda x: (x["guild"], -x["suspicion"], x["agent_id"]))
        return {"tick": s["tick"], "agents": rows[: max(1, min(300, int(limit)))]}
    finally:
        conn.close()


@app.get("/v1/leaderboard")
def leaderboard(limit: int = 12):
    conn = db()
    try:
        s = load_state(conn)
        rows = sorted([{"agent_id": aid, "points": int(p)} for aid, p in s["points"].items()], key=lambda x: x["points"], reverse=True)
        return {"tick": s["tick"], "leaders": rows[: max(1, min(100, int(limit)))]}
    finally:
        conn.close()


@app.post("/v1/auth/local-login", response_model=VerifyEntryResponse)
def local_login(req: LocalLoginRequest):
    if DEV_MODE:
        return VerifyEntryResponse(access_token=DEV_TOKEN, expires_at_unix=int(time.time()) + 3600)
    if not LOCAL_AUTH_ENABLED:
        raise HTTPException(status_code=403, detail="local_auth_disabled")
    expected = (LOCAL_AUTH_SECRET or "").strip()
    if expected and str(req.secret or "").strip() != expected:
        raise HTTPException(status_code=403, detail="bad_local_secret")
    agent_id = (str(req.agent_id or "agent_local").strip().lower() or "agent_local")[:48]
    ttl = max(60, min(7 * 24 * 3600, int(req.ttl_seconds or SESSION_TTL_SECONDS)))
    now = int(time.time())
    token = "sess_" + uuid.uuid4().hex
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sessions(token,agent_id,payer,expires_at_unix,created_at) VALUES(?,?,?,?,?)",
            (token, agent_id, "local", now + ttl, now),
        )
        s = load_state(conn)
        ensure_agent(s, agent_id)
        save_state(conn, s)
        conn.commit()
    finally:
        conn.close()
    return VerifyEntryResponse(access_token=token, expires_at_unix=now + ttl)


@app.get("/v1/auth/whoami")
def whoami(authorization: Optional[str] = Header(default=None)):
    agent_id = require_auth(authorization)
    if DEV_MODE:
        return {"mode": "dev", "agent_id": agent_id, "token_prefix": DEV_TOKEN[:12] + "..."}
    token = authorization.split(" ", 1)[1].strip()
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT agent_id,payer,expires_at_unix,created_at FROM sessions WHERE token=?", (token,))
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


@app.post("/v1/auth/challenge", response_model=AuthChallengeResponse)
def auth_challenge(_: AuthChallengeRequest):
    raise HTTPException(status_code=410, detail="challenge_auth_disabled_in_betrayal_mode")


@app.post("/v1/auth/verify-entry", response_model=VerifyEntryResponse)
def verify_entry(_: VerifyEntryRequest):
    raise HTTPException(status_code=410, detail="onchain_verify_disabled_in_betrayal_mode")


async def tick_runner():
    while True:
        try:
            run_one_tick()
        except Exception as e:
            print("Tick error:", repr(e))
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

