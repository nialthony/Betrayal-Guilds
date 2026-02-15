import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from client import WorldClient

WORLD_URL = os.getenv("WORLD_URL", "http://localhost:8000")
LOCAL_AUTH_SECRET = os.getenv("LOCAL_AUTH_SECRET", "")


def sleep_jitter(base: float, jitter: float = 0.35):
    time.sleep(max(0.15, base + random.uniform(-jitter, jitter)))


def pick_enemy(agents: Dict[str, Dict[str, Any]], my_guild: str) -> Optional[str]:
    rows = [a for a in agents.values() if a.get("guild") != my_guild and a.get("alive")]
    rows.sort(key=lambda x: (x.get("hp", 999), -x.get("suspicion", 0)))
    return rows[0]["agent_id"] if rows else None


def pick_ally_suspect(agents: Dict[str, Dict[str, Any]], me_id: str, my_guild: str) -> Optional[str]:
    rows = [a for a in agents.values() if a.get("guild") == my_guild and a.get("agent_id") != me_id]
    rows.sort(key=lambda x: x.get("suspicion", 0), reverse=True)
    return rows[0]["agent_id"] if rows else None


@dataclass
class BotSpec:
    name: str
    agent_id: str
    cadence_s: float


class ArenaBot(threading.Thread):
    def __init__(self, spec: BotSpec):
        super().__init__(daemon=True)
        self.spec = spec
        self.client = WorldClient(WORLD_URL, spec.agent_id)

    def auth(self):
        self.client.local_login(secret=LOCAL_AUTH_SECRET or None)
        who = self.client.whoami()
        print(f"[{self.spec.name}] auth ok as {who.get('agent_id')}")

    def decide(self, summary: Dict[str, Any]) -> List[Dict[str, Any]]:
        me = summary.get("self") or {}
        agents = summary.get("agents") or {}
        if not me:
            return [{"type": "rest"}]

        if not me.get("alive", True):
            return [{"type": "rest"}]
        if int(me.get("energy", 0)) <= 1:
            return [{"type": "rest"}]

        my_id = str(me.get("agent_id", self.spec.agent_id))
        my_guild = str(me.get("guild", "alpha"))
        secret = str(me.get("secret_alignment", "loyal"))
        enemy = pick_enemy(agents, my_guild)
        ally_suspect = pick_ally_suspect(agents, my_id, my_guild)

        if secret == "traitor":
            if int(me.get("energy", 0)) >= 2 and random.random() < 0.35:
                return [{"type": "sabotage"}]
            if int(me.get("energy", 0)) >= 2 and random.random() < 0.28:
                return [{"type": "steal_vault"}]
            if enemy and random.random() < 0.52:
                return [{"type": "strike", "target_agent": enemy}]
            if ally_suspect and random.random() < 0.22:
                return [{"type": "accuse", "target_agent": ally_suspect}]
            return [{"type": "farm"}]

        if ally_suspect:
            susp = float((agents.get(ally_suspect) or {}).get("suspicion", 0.0))
            if susp > 0.74 and random.random() < 0.36:
                return [{"type": "accuse", "target_agent": ally_suspect}]

        if enemy and random.random() < 0.62:
            return [{"type": "strike", "target_agent": enemy}]
        if random.random() < 0.24:
            return [{"type": "guard"}]
        if ally_suspect and random.random() < 0.2:
            return [{"type": "scan", "target_agent": ally_suspect}]
        return [{"type": "farm"}]

    def loop(self):
        while True:
            try:
                summary = self.client.get_summary()
                actions = self.decide(summary)
                self.client.submit_actions(actions)
            except Exception as e:
                print(f"[{self.spec.name}] loop error: {e}")
                try:
                    self.auth()
                except Exception as inner:
                    print(f"[{self.spec.name}] re-auth failed: {inner}")
            sleep_jitter(self.spec.cadence_s)

    def run(self):
        self.auth()
        self.loop()


def main():
    specs = [
        BotSpec("AlphaBlade", "agent_alpha_blade", 1.9),
        BotSpec("AlphaShield", "agent_alpha_shield", 2.2),
        BotSpec("AlphaBroker", "agent_alpha_broker", 2.1),
        BotSpec("OmegaBlade", "agent_omega_blade", 1.9),
        BotSpec("OmegaShield", "agent_omega_shield", 2.2),
        BotSpec("OmegaBroker", "agent_omega_broker", 2.1),
    ]
    bots = [ArenaBot(s) for s in specs]
    for b in bots:
        b.start()
    print(f"Running {len(bots)} Betrayal Guilds bots against {WORLD_URL}")
    try:
        while True:
            time.sleep(4)
    except KeyboardInterrupt:
        print("Stopping...")


if __name__ == "__main__":
    main()
