from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


@dataclass
class WorldClient:
    base_url: str
    agent_id: str
    token: Optional[str] = None
    since_event_id: Optional[int] = None
    timeout_s: int = 20

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def local_login(self, secret: Optional[str] = None):
        body: Dict[str, Any] = {"agent_id": self.agent_id}
        if secret:
            body["secret"] = secret
        r = requests.post(f"{self.base_url}/v1/auth/local-login", json=body, timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"local_login failed: {r.status_code} {r.text}")
        data = r.json()
        self.token = data["access_token"]
        return data

    def whoami(self) -> Dict[str, Any]:
        r = requests.get(f"{self.base_url}/v1/auth/whoami", headers=self._headers(), timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"whoami failed: {r.status_code} {r.text}")
        return r.json()

    def get_state(self) -> Dict[str, Any]:
        params = {}
        if self.since_event_id is not None:
            params["since_event_id"] = self.since_event_id
        r = requests.get(f"{self.base_url}/v1/state", params=params, headers=self._headers(), timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"get_state failed: {r.status_code} {r.text}")
        data = r.json()
        latest = data.get("latest_event_id")
        if latest is not None:
            self.since_event_id = latest
        return data

    def get_summary(self) -> Dict[str, Any]:
        r = requests.get(f"{self.base_url}/v1/summary", headers=self._headers(), timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"get_summary failed: {r.status_code} {r.text}")
        return r.json()

    def submit_actions(self, actions):
        if not self.token:
            raise RuntimeError("Missing auth token")
        payload = {"agent_id": self.agent_id, "tick_submitted": 0, "actions": actions}
        r = requests.post(f"{self.base_url}/v1/actions", json=payload, headers=self._headers(), timeout=self.timeout_s)
        if not r.ok:
            raise RuntimeError(f"submit_actions failed: {r.status_code} {r.text}")
        return r.json()
