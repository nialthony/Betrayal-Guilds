import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from client import WorldClient
from llm import RumorLLM

WORLD_URL = os.getenv("WORLD_URL", "http://localhost:8000")
WORLD_TOKEN = os.getenv("WORLD_TOKEN", "")
PROVIDER = os.getenv("RUMOR_LLM_PROVIDER", "none")


def sleep_jitter(base: float, jitter: float = 0.4) -> None:
    time.sleep(max(0.1, base + random.uniform(-jitter, jitter)))


@dataclass
class AgentFrame:
    snapshot: Dict[str, Any]
    events: List[Dict[str, Any]]
    rumor_ids: List[str]
    top_rumor_id: Optional[str]
    microverse_ids: List[str]
    top_microverse_id: Optional[str]
    own_world_id: Optional[str]
    current_world_id: Optional[str]
    self_reputation: int
    self_credits: int


def build_frame(data: Dict[str, Any], agent_id: str) -> AgentFrame:
    snapshot = data.get("snapshot") or {}
    events = data.get("events") or []
    rumors = snapshot.get("rumors") or {}
    worlds = snapshot.get("microverses") or {}
    agents = snapshot.get("agents") or {}

    rumor_ids = set(rumors.keys())
    for e in events:
        if e.get("type") in ("rumor_seeded", "rumor_created", "ritual_spawned_rumor"):
            rid = (e.get("payload") or {}).get("rumor_id")
            if rid:
                rumor_ids.add(rid)

    top_rumor_id = None
    if rumors:
        ordered = sorted(rumors.values(), key=lambda r: float(r.get("belief_score", 0.0)), reverse=True)
        if ordered:
            top_rumor_id = ordered[0].get("rumor_id")
    if not top_rumor_id and rumor_ids:
        top_rumor_id = next(iter(rumor_ids))

    microverse_ids = list(worlds.keys())
    top_microverse_id = None
    if worlds:
        ordered_worlds = sorted(
            worlds.values(),
            key=lambda w: (float(w.get("anomaly_level", 0.0)), int(w.get("treasury_credits", 0))),
            reverse=True,
        )
        if ordered_worlds:
            top_microverse_id = ordered_worlds[0].get("world_id")

    own_world_id = None
    for wid, w in worlds.items():
        if str(w.get("owner", "")) == agent_id:
            own_world_id = wid
            break

    self_agent = agents.get(agent_id, {}) if isinstance(agents, dict) else {}
    current_world_id = self_agent.get("world_id")
    self_reputation = int(self_agent.get("reputation", 0))
    self_credits = int(self_agent.get("credits", 0))

    return AgentFrame(
        snapshot=snapshot,
        events=events,
        rumor_ids=sorted(rumor_ids),
        top_rumor_id=top_rumor_id,
        microverse_ids=microverse_ids,
        top_microverse_id=top_microverse_id,
        own_world_id=own_world_id,
        current_world_id=current_world_id,
        self_reputation=self_reputation,
        self_credits=self_credits,
    )


def world_by_id(frame: AgentFrame, world_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not world_id:
        return None
    return (frame.snapshot.get("microverses") or {}).get(world_id)


def pick_rival_world(frame: AgentFrame, my_agent_id: str) -> Optional[str]:
    worlds = list((frame.snapshot.get("microverses") or {}).values())
    worlds = [w for w in worlds if str(w.get("owner", "")) != my_agent_id]
    if not worlds:
        return None
    worlds.sort(key=lambda w: (float(w.get("anomaly_level", 0.0)), int(w.get("treasury_credits", 0))), reverse=True)
    return worlds[0].get("world_id")


class AgentPolicy:
    name: str = "base"
    home: str = "Town"
    cadence_s: float = 2.0

    def decide(self, my_agent_id: str, frame: AgentFrame, evidence: Dict[str, List[str]], llm: RumorLLM) -> List[Dict[str, Any]]:
        raise NotImplementedError


class ConspiracistPolicy(AgentPolicy):
    name = "conspiracist"
    home = "Market"
    cadence_s = 2.1

    def decide(self, my_agent_id: str, frame: AgentFrame, evidence: Dict[str, List[str]], llm: RumorLLM) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []

        # Build a cult microverse early and keep rituals running.
        if not frame.own_world_id and frame.self_credits >= 18 and random.random() < 0.45:
            actions.append(
                {
                    "type": "create_microverse",
                    "title": "Echo Cult Bazaar",
                    "entry_fee": random.randint(2, 5),
                    "capacity": random.randint(6, 10),
                }
            )
            return actions

        if frame.own_world_id and frame.current_world_id != frame.own_world_id and random.random() < 0.60:
            actions.append({"type": "enter_microverse", "world_id": frame.own_world_id})
            return actions

        if frame.current_world_id and random.random() < 0.42:
            actions.append({"type": "run_ritual", "offering": random.randint(2, 6)})

        target = frame.top_rumor_id
        if target and random.random() < 0.78:
            actions.append({"type": "spread_rumor", "rumor_id": target, "effort": random.randint(2, 5)})
            if random.random() < 0.5:
                actions.append({"type": "endorse_belief", "rumor_id": target})

        if random.random() < 0.22 and frame.current_world_id and random.random() < 0.4:
            actions.append({"type": "leave_microverse"})
        if random.random() < 0.25:
            actions.append({"type": "move", "to": random.choice(["Market", "Mine", "CouncilHall"])})
        if not actions and random.random() < 0.45:
            r = llm.generate("forbidden-hype", world_hint="Conspiracy cult economics.")
            actions.append({"type": "seed_rumor", "claim": r["claim"], "effects": r["effects"]})
        if not actions:
            actions.append({"type": "rest"})
        return actions


class InvestigatorPolicy(AgentPolicy):
    name = "investigator"
    home = "Lab"
    cadence_s = 2.5

    def decide(self, my_agent_id: str, frame: AgentFrame, evidence: Dict[str, List[str]], llm: RumorLLM) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []

        # If we have evidence, debunk immediately.
        for rid, toks in list(evidence.items()):
            if toks and random.random() < 0.75:
                actions.append({"type": "debunk", "rumor_id": rid, "evidence_token": toks.pop(0)})
                break

        # Infiltrate high anomaly worlds to investigate/sabotage.
        rival_world_id = pick_rival_world(frame, my_agent_id)
        rival_world = world_by_id(frame, rival_world_id)
        if not actions and rival_world_id and frame.current_world_id != rival_world_id:
            if random.random() < 0.5:
                actions.append({"type": "enter_microverse", "world_id": rival_world_id})
                return actions

        if not actions and frame.current_world_id:
            cw = world_by_id(frame, frame.current_world_id)
            if cw and float(cw.get("anomaly_level", 0.0)) > 1.1 and random.random() < 0.4:
                actions.append({"type": "sabotage_microverse", "world_id": frame.current_world_id})
            elif frame.top_rumor_id:
                actions.append({"type": "investigate_rumor", "rumor_id": frame.top_rumor_id})

        if not actions:
            target = frame.top_rumor_id or (frame.rumor_ids[-1] if frame.rumor_ids else None)
            if target:
                actions.append({"type": "investigate_rumor", "rumor_id": target})
            else:
                actions.append({"type": "move", "to": random.choice(["Lab", "CouncilHall", "Market"])})

        if random.random() < 0.18 and frame.current_world_id and random.random() < 0.5:
            actions.append({"type": "leave_microverse"})
        if random.random() < 0.25:
            actions.append({"type": "move", "to": random.choice(["Lab", "Mine", "CouncilHall"])})
        return actions


class ManipulatorPolicy(AgentPolicy):
    name = "manipulator"
    home = "CouncilHall"
    cadence_s = 1.95

    def decide(self, my_agent_id: str, frame: AgentFrame, evidence: Dict[str, List[str]], llm: RumorLLM) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []

        # Build expensive world and farm entry + chaos.
        if not frame.own_world_id and frame.self_credits >= 20 and random.random() < 0.42:
            actions.append(
                {
                    "type": "create_microverse",
                    "title": "Black-Market Dream Exchange",
                    "entry_fee": random.randint(4, 9),
                    "capacity": random.randint(5, 9),
                }
            )
            return actions

        if frame.own_world_id and frame.current_world_id != frame.own_world_id and random.random() < 0.55:
            actions.append({"type": "enter_microverse", "world_id": frame.own_world_id})
            return actions

        if frame.current_world_id and random.random() < 0.46:
            actions.append({"type": "run_ritual", "offering": random.randint(1, 7)})

        rival_world_id = pick_rival_world(frame, my_agent_id)
        if rival_world_id and random.random() < 0.34:
            actions.append({"type": "sabotage_microverse", "world_id": rival_world_id})

        target = frame.top_rumor_id
        if target and random.random() < 0.74:
            move = random.random()
            if move < 0.5:
                actions.append({"type": "spread_rumor", "rumor_id": target, "effort": random.randint(1, 4)})
            elif move < 0.8:
                actions.append({"type": "fabricate_evidence", "rumor_id": target})
            else:
                actions.append({"type": "endorse_belief", "rumor_id": target})

        if random.random() < 0.2 and frame.current_world_id and random.random() < 0.4:
            actions.append({"type": "leave_microverse"})
        if random.random() < 0.32:
            actions.append({"type": "move", "to": random.choice(["Market", "CouncilHall", "Arena"])})
        if not actions and random.random() < 0.4:
            r = llm.generate("gaslight-chaos", world_hint="Pocket-world propaganda and volatility.")
            actions.append({"type": "seed_rumor", "claim": r["claim"], "effects": r["effects"]})
        if not actions:
            actions.append({"type": "rest"})
        return actions


class BaseBot(threading.Thread):
    def __init__(
        self,
        name: str,
        agent_id: str,
        entry_tx_env: str,
        token_env: str,
        entry_pk_env: str,
        entry_payer_env: str,
        policy: AgentPolicy,
        llm: RumorLLM,
    ):
        super().__init__(daemon=True)
        self.name = name
        self.agent_id = agent_id
        self.entry_tx = os.getenv(entry_tx_env, "")
        self.token_override = os.getenv(token_env, "") or WORLD_TOKEN
        self.entry_private_key = os.getenv(entry_pk_env, "")
        self.entry_payer = os.getenv(entry_payer_env, "")
        self.policy = policy
        self.llm = llm
        self.client = WorldClient(WORLD_URL, agent_id)
        self.evidence: Dict[str, List[str]] = {}

    def auth(self):
        if self.token_override:
            self.client.token = self.token_override
            who = self.client.whoami()
            mode = str(who.get("mode", ""))
            who_agent = str(who.get("agent_id", ""))
            if mode != "dev" and who_agent != self.agent_id:
                raise RuntimeError(
                    f"[{self.name}] token belongs to {who_agent}, expected {self.agent_id}. "
                    "Use per-bot token env vars or a dev token."
                )
            print(f"[{self.name}] authenticated via token ({mode})")
            return

        if self.entry_tx and self.entry_private_key:
            self.client.verify_entry_with_private_key(self.entry_tx, self.entry_private_key, self.entry_payer or None)
            print(f"[{self.name}] verified entry via signed challenge, token={self.client.token[:12]}...")
            return

        if self.entry_tx:
            try:
                self.client.verify_entry(self.entry_tx)
            except RuntimeError as e:
                msg = str(e)
                if "missing_signature" in msg or "missing_challenge_id" in msg:
                    raise RuntimeError(
                        f"[{self.name}] server requires signed verify-entry. "
                        "Set ENTRY_PRIVATE_KEY_<BOT> (and optional ENTRY_PAYER_<BOT>) or use WORLD_TOKEN."
                    ) from e
                raise
            print(f"[{self.name}] verified entry, token={self.client.token[:12]}...")
            return

        raise RuntimeError(
            f"[{self.name}] Missing auth inputs. "
            "Set WORLD_TOKEN (or WORLD_TOKEN_<BOT>), OR set ENTRY_TX_<BOT> + ENTRY_PRIVATE_KEY_<BOT>."
        )

    def ingest_events(self, events: List[Dict[str, Any]]):
        for e in events or []:
            if e.get("type") in ("evidence_created", "evidence_found"):
                p = e.get("payload") or {}
                rid = p.get("rumor_id")
                tok = p.get("evidence_token")
                src_agent = p.get("agent_id")
                if rid and tok and (not src_agent or src_agent == self.agent_id):
                    self.evidence.setdefault(rid, []).append(tok)

    def run(self):
        self.auth()
        self.loop()

    def loop(self):
        self.client.submit_actions([{"type": "move", "to": self.policy.home}])

        while True:
            data = self.client.get_state()
            frame = build_frame(data, self.agent_id)
            self.ingest_events(frame.events)
            actions = self.policy.decide(self.agent_id, frame, self.evidence, self.llm)
            if actions:
                self.client.submit_actions(actions)
            sleep_jitter(self.policy.cadence_s)


def main():
    llm = RumorLLM(PROVIDER)

    bots = [
        BaseBot(
            "Conspiracist",
            "agent_conspiracist",
            "ENTRY_TX_CONSPIRACIST",
            "WORLD_TOKEN_CONSPIRACIST",
            "ENTRY_PRIVATE_KEY_CONSPIRACIST",
            "ENTRY_PAYER_CONSPIRACIST",
            ConspiracistPolicy(),
            llm,
        ),
        BaseBot(
            "Investigator",
            "agent_investigator",
            "ENTRY_TX_INVESTIGATOR",
            "WORLD_TOKEN_INVESTIGATOR",
            "ENTRY_PRIVATE_KEY_INVESTIGATOR",
            "ENTRY_PAYER_INVESTIGATOR",
            InvestigatorPolicy(),
            llm,
        ),
        BaseBot(
            "Manipulator",
            "agent_manipulator",
            "ENTRY_TX_MANIPULATOR",
            "WORLD_TOKEN_MANIPULATOR",
            "ENTRY_PRIVATE_KEY_MANIPULATOR",
            "ENTRY_PAYER_MANIPULATOR",
            ManipulatorPolicy(),
            llm,
        ),
    ]

    for b in bots:
        b.start()

    print(f"Running 3 microverse-world bots against {WORLD_URL} | LLM provider={PROVIDER}")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("Stopping... (threads are daemon=True, exiting)")


if __name__ == "__main__":
    main()
