"""
WhatsApp messaging via the Meta WhatsApp Cloud API (test number).

- send_whatsapp_text(): plain text
- send_whatsapp_buttons(): interactive reply buttons (max 3) — reduces ambiguity
  when many bookings/requests are open at once (each button carries a booking id)
- send_admin_alert(): titled text to the admin number

Note on free-form text: WhatsApp only delivers business-initiated free-form
text inside the 24-hour customer-service window (i.e. after the recipient
messages the number first). Replies to an incoming guest message are always
inside that window, so they deliver fine.
"""
import logging
from typing import List, Dict, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _digits(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


def _api_url() -> str:
    return (
        f"https://graph.facebook.com/{settings.META_API_VERSION}"
        f"/{settings.META_PHONE_NUMBER_ID}/messages"
    )


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


async def _post_message(payload: dict) -> bool:
    if not (settings.META_ACCESS_TOKEN and settings.META_PHONE_NUMBER_ID):
        logger.warning("Meta Cloud API not configured — skipping: %s", str(payload)[:80])
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(_api_url(), json=payload, headers=_headers())
            if resp.status_code not in (200, 201):
                logger.error("Meta Cloud API returned %s: %s", resp.status_code, resp.text[:300])
                return False
            return True
    except httpx.HTTPError as exc:
        logger.error("Meta Cloud API request failed: %s", exc)
        return False


async def send_whatsapp_text(to: str, body: str) -> bool:
    """Send a plain WhatsApp text message to `to` via the Meta Cloud API."""
    if not to:
        return False
    payload = {
        "messaging_product": "whatsapp",
        "to": _digits(to),
        "type": "text",
        "text": {"body": body},
    }
    return await _post_message(payload)


async def send_whatsapp_buttons(
    to: str,
    body: str,
    buttons: List[Dict[str, str]],
    footer: Optional[str] = None,
) -> bool:
    """Send an interactive message with up to 3 reply buttons.

    Each button: {"id": "pay_yes_<booking_id>", "title": "Yes, paid"}
    Title max 20 chars. Falls back to plain text listing the options if send fails.
    """
    if not to:
        return False

    trimmed = []
    for b in buttons[:3]:
        title = (b.get("title") or "")[:20]
        bid = (b.get("id") or "")[:256]
        if title and bid:
            trimmed.append({"type": "reply", "reply": {"id": bid, "title": title}})

    if not trimmed:
        return await send_whatsapp_text(to, body)

    interactive: dict = {
        "type": "button",
        "body": {"text": body[:1024]},
        "action": {"buttons": trimmed},
    }
    if footer:
        interactive["footer"] = {"text": footer[:60]}

    payload = {
        "messaging_product": "whatsapp",
        "to": _digits(to),
        "type": "interactive",
        "interactive": interactive,
    }
    ok = await _post_message(payload)
    if ok:
        return True

    # Fallback: plain text with the same choices spelled out
    lines = [body, "", "Reply with:"]
    for b in trimmed:
        lines.append(f"  • {b['reply']['title']}")
    return await send_whatsapp_text(to, "\n".join(lines))


async def send_admin_alert(
    message: str,
    title: str = "Booking Alert",
    buttons: Optional[List[Dict[str, str]]] = None,
) -> bool:
    """Send a WhatsApp alert to the admin number, optionally with reply buttons."""
    if not settings.ADMIN_WHATSAPP_NUMBER:
        logger.warning("ADMIN_WHATSAPP_NUMBER not set — skipping alert: %s", message[:80])
        return False
    body = f"*{title}*\n\n{message}"
    if buttons:
        return await send_whatsapp_buttons(settings.ADMIN_WHATSAPP_NUMBER, body, buttons)
    return await send_whatsapp_text(settings.ADMIN_WHATSAPP_NUMBER, body)
