"""
Wraps calls to your opencode LLM endpoint (OpenAI-compatible chat completions format).
If your opencode provider uses a different response shape, adjust `_extract_text`.
"""
import httpx
from app.config import settings


async def call_llm(system_prompt: str, user_message: str, history: list[dict] | None = None) -> str:
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    headers = {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": 0.3,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{settings.LLM_API_BASE}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return _extract_text(data)


def _extract_text(data: dict) -> str:
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return "Sorry, I couldn't process that right now."
