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

    def create_auth_challenge(self, payer: str) -> Dict[str, Any]:
        r = requests.post(
            f"{self.base_url}/v1/auth/challenge",
            json={"payer": payer},
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"auth_challenge failed: {r.status_code} {r.text}")
        return r.json()

    def verify_entry(
        self,
        tx_hash: str,
        payer: Optional[str] = None,
        challenge_id: Optional[str] = None,
        signature: Optional[str] = None,
    ):
        body: Dict[str, Any] = {"tx_hash": tx_hash}
        if payer:
            body["payer"] = payer
        if challenge_id:
            body["challenge_id"] = challenge_id
        if signature:
            body["signature"] = signature
        r = requests.post(
            f"{self.base_url}/v1/auth/verify-entry",
            json=body,
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"verify_entry failed: {r.status_code} {r.text}")
        data = r.json()
        self.token = data["access_token"]
        return data

    def verify_entry_with_private_key(self, tx_hash: str, private_key: str, payer: Optional[str] = None):
        from eth_account import Account
        from eth_account.messages import encode_defunct

        acct = Account.from_key(private_key)
        payer_addr = payer or acct.address
        ch = self.create_auth_challenge(payer_addr)
        signed = Account.sign_message(encode_defunct(text=ch["message"]), private_key=private_key)
        signature = signed.signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature
        return self.verify_entry(
            tx_hash=tx_hash,
            payer=payer_addr,
            challenge_id=ch["challenge_id"],
            signature=signature,
        )

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

    def whoami(self) -> Dict[str, Any]:
        if not self.token:
            raise RuntimeError("Missing auth token")
        r = requests.get(
            f"{self.base_url}/v1/auth/whoami",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=self.timeout_s,
        )
        if not r.ok:
            raise RuntimeError(f"whoami failed: {r.status_code} {r.text}")
        return r.json()

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

