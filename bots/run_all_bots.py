import os, time, random, threading
from typing import Dict, Any, List, Optional

from client import WorldClient
from llm import RumorLLM

WORLD_URL = os.getenv("WORLD_URL", "http://localhost:8000")
WORLD_TOKEN = os.getenv("WORLD_TOKEN", "")
PROVIDER = os.getenv("RUMOR_LLM_PROVIDER", "none")

def sleep_jitter(base: float, jitter: float = 0.4) -> None:
    time.sleep(max(0.1, base + random.uniform(-jitter, jitter)))

def extract_rumor_ids(events: List[Dict[str, Any]]) -> List[str]:
    ids = []
    for e in events or []:
        if e.get("type") in ("rumor_seeded", "rumor_created"):
            rid = (e.get("payload") or {}).get("rumor_id")
            if rid:
                ids.append(rid)
    return ids

class BaseBot(threading.Thread):
    def __init__(self, name: str, agent_id: str, entry_tx_env: str, llm: RumorLLM):
        super().__init__(daemon=True)
        self.name = name
        self.agent_id = agent_id
        self.entry_tx = os.getenv(entry_tx_env, "")
        self.llm = llm
        self.client = WorldClient(WORLD_URL, agent_id)

    def auth(self):
        if self.entry_tx:
            self.client.verify_entry(self.entry_tx)
            print(f"[{self.name}] verified entry, token={self.client.token[:12]}...")
            return
        raise RuntimeError(
            f"[{self.name}] Missing ENTRY_TX env var (got empty). "
            "Set ENTRY_TX_CONSPIRACIST / ENTRY_TX_INVESTIGATOR / ENTRY_TX_MANIPULATOR."
        )

    def run(self):
        self.auth()
        self.loop()

    def loop(self):
        raise NotImplementedError

class ConspiracistBot(BaseBot):
    def loop(self):
        self.client.submit_actions([{"type": "move", "to": "Market"}])

        while True:
            state = self.client.get_state()
            events = state.get("events", [])
            rumor_ids = extract_rumor_ids(events)

            actions = []

            # Seed fresh weirdness fairly often
            if random.random() < 0.40:
                r = self.llm.generate("paranoid-spooky", world_hint="Prefer Mine/ore effects.")
                actions.append({"type": "seed_rumor", "claim": r["claim"], "effects": r["effects"]})

            # Pump a rumor if any exist
            if rumor_ids and random.random() < 0.80:
                rid = random.choice(rumor_ids)
                actions.append({"type": "spread_rumor", "rumor_id": rid, "effort": random.randint(2, 4)})
                actions.append({"type": "endorse_belief", "rumor_id": rid})

            # Wander between influence centers
            if random.random() < 0.30:
                actions.append({"type": "move", "to": random.choice(["Market", "Mine"])})

            if actions:
                self.client.submit_actions(actions)

            sleep_jitter(2.2)

class InvestigatorBot(BaseBot):
    def loop(self):
        self.client.submit_actions([{"type": "move", "to": "Lab"}])
        evidence: Dict[str, List[str]] = {}

        while True:
            state = self.client.get_state()
            events = state.get("events", [])
            rumor_ids = extract_rumor_ids(events)

            # Collect evidence tokens if your server emits them
            for e in events:
                if e.get("type") in ("evidence_created", "evidence_found"):
                    p = e.get("payload") or {}
                    rid = p.get("rumor_id")
                    tok = p.get("evidence_token")
                    if rid and tok:
                        evidence.setdefault(rid, []).append(tok)

            actions = []

            # Debunk when possible
            for rid, toks in list(evidence.items()):
                if toks and random.random() < 0.70:
                    actions.append({"type": "debunk", "rumor_id": rid, "evidence_token": toks.pop(0)})
                    break

            # Otherwise investigate something recent
            if not actions:
                if rumor_ids:
                    target = rumor_ids[-1]
                    actions.append({"type": "investigate_rumor", "rumor_id": target})
                else:
                    actions.append({"type": "move", "to": random.choice(["Market", "CouncilHall", "Mine"])})

            if actions:
                self.client.submit_actions(actions)

            sleep_jitter(2.6)

class ManipulatorBot(BaseBot):
    def loop(self):
        self.client.submit_actions([{"type": "move", "to": "CouncilHall"}])

        known: Dict[str, Dict[str, Any]] = {}

        while True:
            state = self.client.get_state()
            events = state.get("events", [])

            for e in events:
                if e.get("type") in ("rumor_seeded", "rumor_created"):
                    p = e.get("payload") or {}
                    rid = p.get("rumor_id")
                    if rid:
                        known[rid] = {"claim": p.get("claim", ""), "effects": p.get("effects", {})}

            actions = []

            # Seed counter-rumors or chaos rumors
            if random.random() < 0.28:
                r = self.llm.generate("gaslight-chaos", world_hint="Prefer Market/Council effects and volatility.")
                actions.append({"type": "seed_rumor", "claim": r["claim"], "effects": r["effects"]})

            # Play both sides: spread OR fabricate evidence
            if known and random.random() < 0.85:
                rid = random.choice(list(known.keys()))
                move = random.random()
                if move < 0.45:
                    actions.append({"type": "spread_rumor", "rumor_id": rid, "effort": random.randint(1, 3)})
                elif move < 0.75:
                    actions.append({"type": "fabricate_evidence", "rumor_id": rid})
                else:
                    actions.append({"type": "endorse_belief", "rumor_id": rid})

            if random.random() < 0.35:
                actions.append({"type": "move", "to": random.choice(["Market", "CouncilHall", "Arena"])})

            if actions:
                self.client.submit_actions(actions)

            sleep_jitter(2.0)

def main():
    llm = RumorLLM(PROVIDER)

    bots = [
        ConspiracistBot("Conspiracist", "agent_conspiracist", "ENTRY_TX_CONSPIRACIST", llm),
        InvestigatorBot("Investigator", "agent_investigator", "ENTRY_TX_INVESTIGATOR", llm),
        ManipulatorBot("Manipulator", "agent_manipulator", "ENTRY_TX_MANIPULATOR", llm),
    ]

    for b in bots:
        b.start()

    print(f"Running 3 bots against {WORLD_URL} | LLM provider={PROVIDER}")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("Stopping... (threads are daemon=True, exiting)")

if __name__ == "__main__":
    main()
