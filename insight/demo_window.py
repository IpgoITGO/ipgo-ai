"""Deployment-only demo deadline. No provider request is made after expiry."""
import json
import os
from datetime import UTC, datetime

SNAPSHOT = "gpt-4o-mini-2024-07-18"


def options(system: str, user: str, max_tokens: int, model: str, *, now: datetime | None = None) -> dict:
    expiry = os.getenv("IPGO_LLM_EXPIRES_AT", "")
    if not expiry:
        return {}
    end = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    if end.tzinfo is None or (now or datetime.now(UTC)) >= end:
        raise ValueError("LLM demo window closed")
    if model not in ("gpt-4o-mini", SNAPSHOT) or not 1 <= max_tokens <= 400:
        raise ValueError("LLM demo model or output limit rejected")
    if os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") != "https://api.openai.com/v1":
        raise ValueError("LLM demo origin rejected")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 1024 > 16000:
        raise ValueError("LLM demo input limit exceeded")
    return {"model": SNAPSHOT, "n": 1, "service_tier": "default"}
