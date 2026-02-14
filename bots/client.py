import json
import requests
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

@dataclass
class WorldClient:
    base_url: str
    agent_id: str
    token: Optional[str] = None
    since_event_id: Optional[int] = None
    timeout_s: int = 20

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def verify_entry(self, tx_hash: str):
        r = requests.post(
            f"{self.base_url}/v1/auth/verify-entry",
            json={"tx_hash": tx_hash},
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"verify_entry failed: {r.status_code} {r.text}")
        data = r.json()
        self.token = data["access_token"]
        return data

    def get_state(self) -> Dict[str, Any]:
        params = {}
        if self.since_event_id is not None:
            params["since_event_id"] = self.since_event_id
        r = requests.get(f"{self.base_url}/v1/state", params=params, timeout=self.timeout_s)
        r.raise_for_status()
        data = r.json()
        latest = data.get("latest_event_id")
        if latest is not None:
            self.since_event_id = latest
        return data

    def submit_actions(self, actions):
        if not self.token:
            raise RuntimeError("Missing auth token: call verify_entry() first or set WORLD_TOKEN")

        payload = {
            "agent_id": self.agent_id,
            "tick_submitted": 0,
            "actions": actions,
        }

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        r = requests.post(f"{self.base_url}/v1/actions", json=payload, headers=headers, timeout=20)
        if not r.ok:
            raise RuntimeError(f"submit_actions failed: {r.status_code} {r.text}")
        return r.json()

