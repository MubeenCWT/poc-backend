"""
WhatsApp messaging via the Meta WhatsApp Cloud API (test number).

- send_whatsapp_text(): generic — used to reply to guests on the webhook.
- send_admin_alert(): one-way alert to the admin number, with a bold title.

Note on free-form text: WhatsApp only delivers business-initiated free-form
text inside the 24-hour customer-service window (i.e. after the recipient
messages the number first). Replies to an incoming guest message are always
inside that window, so they deliver fine.
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _digits(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


async def send_whatsapp_text(to: str, body: str) -> bool:
    """Send a plain WhatsApp text message to `to` via the Meta Cloud API."""
    if not (settings.META_ACCESS_TOKEN and settings.META_PHONE_NUMBER_ID and to):
        logger.warning(
            "Meta Cloud API not configured (META_ACCESS_TOKEN / META_PHONE_NUMBER_ID) "
            "or empty recipient — skipping message: %s",
            body[:80],
        )
        return False

    url = (
        f"https://graph.facebook.com/{settings.META_API_VERSION}"
        f"/{settings.META_PHONE_NUMBER_ID}/messages"
    )
    headers = {
        "Authorization": f"Bearer {settings.META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": _digits(to),
        "type": "text",
        "text": {"body": body},
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code not in (200, 201):
                logger.error("Meta Cloud API returned %s: %s", resp.status_code, resp.text[:300])
                return False
            return True
    except httpx.HTTPError as exc:
        logger.error("Meta Cloud API request failed: %s", exc)
        return False


async def send_admin_alert(message: str, title: str = "DAR Alert") -> bool:
    """Send a one-way WhatsApp alert to the admin number."""
    if not settings.ADMIN_WHATSAPP_NUMBER:
        logger.warning("ADMIN_WHATSAPP_NUMBER not set — skipping alert: %s", message[:80])
        return False
    return await send_whatsapp_text(settings.ADMIN_WHATSAPP_NUMBER, f"*{title}*\n\n{message}")
