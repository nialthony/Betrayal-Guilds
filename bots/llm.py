import os, json, random
from typing import Dict, Any, Optional

def _clamp_effects(effects: Dict[str, Any]) -> Dict[str, float]:
    """Keep effects small + numeric to avoid breaking your sim."""
    out = {}
    for k, v in (effects or {}).items():
        try:
            fv = float(v)
            out[k] = max(-5.0, min(5.0, fv))
        except Exception:
            continue
    return out

class RumorLLM:
    def __init__(self, provider: str):
        self.provider = provider.lower().strip()

        # Lazy imports so you can run with provider="none"
        self._openai_client = None
        self._gemini_client = None

        if self.provider == "openai":
            from openai import OpenAI
            self._openai_client = OpenAI()  # reads OPENAI_API_KEY from env :contentReference[oaicite:5]{index=5}
        elif self.provider == "gemini":
            from google import genai
            self._gemini_client = genai.Client()  # reads GEMINI_API_KEY from env :contentReference[oaicite:6]{index=6}

    def generate(self, vibe: str, world_hint: str = "") -> Dict[str, Any]:
        if self.provider == "none":
            return self._fallback(vibe)

        prompt = f"""
You are generating a single "Rumor" for a weird persistent multi-agent world.
Return STRICT JSON with keys: claim (string), effects (object of numeric modifiers).
Keep effects small magnitude (between -2 and +4). Avoid long text.
Vibe: {vibe}
World hint: {world_hint}

Example:
{{"claim":"The Mine is haunted.","effects":{{"Mine.gather_risk":0.25,"ore.price":2}}}}
"""

        if self.provider == "openai":
            resp = self._openai_client.responses.create(
                model=os.getenv("OPENAI_MODEL", "gpt-5.2"),
                input=prompt,
            )
            text = getattr(resp, "output_text", None) or ""
            return self._parse(text, vibe)

        if self.provider == "gemini":
            resp = self._gemini_client.models.generate_content(
                model=os.getenv("GEMINI_MODEL", "gemini-3-flash-preview"),
                contents=prompt,
            )
            text = getattr(resp, "text", "") or ""
            return self._parse(text, vibe)

        return self._fallback(vibe)

    def _parse(self, text: str, vibe: str) -> Dict[str, Any]:
        # Try strict JSON first
        try:
            obj = json.loads(text.strip())
            return {
                "claim": str(obj.get("claim", "")).strip()[:200],
                "effects": _clamp_effects(obj.get("effects", {})),
            }
        except Exception:
            return self._fallback(vibe)

    def _fallback(self, vibe: str) -> Dict[str, Any]:
        canned = [
            ("The Mine is haunted. Ore screams when mined.", {"Mine.gather_risk": 0.25, "ore.price": 2}),
            ("The Market is lying about weights. Reality is slippery today.", {"Market.volatility": 0.10}),
            ("Saying 'banana' in the Arena bends odds.", {"Arena.wager_edge": 0.10}),
        ]
        claim, effects = random.choice(canned)
        return {"claim": f"[{vibe}] {claim}", "effects": effects}
