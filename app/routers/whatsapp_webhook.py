"""
Two-way WhatsApp chat via the Meta Cloud API.

  GET  /webhook/whatsapp  -> verification handshake (Meta calls this once)
  POST /webhook/whatsapp  -> incoming guest messages; runs the LangGraph agent
                             and replies on WhatsApp.

Each WhatsApp sender (wa_id) gets its own chatbot session, so conversations are
independent and resumable — the same graph that powers the website widget.
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response

from app.chatbot.graph import chatbot_graph
from app.config import settings
from app.database import SessionLocal
from app.models.models import Booking, ChatbotMessage, ChatbotSession
from app.services.discounts import apply_discount_decision
from app.services.notify import send_whatsapp_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["whatsapp"])

_APPROVE_WORDS = {"approve", "approved", "accept", "accepted"}
_REJECT_WORDS = {"reject", "rejected", "decline", "declined", "deny", "denied"}


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


@router.get("/whatsapp")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    """Meta calls this once when you save the webhook URL in the dashboard."""
    if hub_mode == "subscribe" and hub_verify_token == settings.META_VERIFY_TOKEN:
        return Response(content=hub_challenge or "", media_type="text/plain")
    return Response(content="verification failed", status_code=403)


@router.post("/whatsapp")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive incoming messages. Reply is done in the background so we can
    return 200 immediately (Meta retries if we're slow)."""
    data = await request.json()

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue
                phone = msg.get("from")
                text = (msg.get("text") or {}).get("body", "")
                if phone and text:
                    background_tasks.add_task(_handle_message, phone, text)

    return {"status": "received"}


async def _try_admin_command(text: str, db) -> bool:
    """If `text` is an approve/reject command from the admin, apply the discount
    decision and notify the tenant. Returns True when handled (so the normal
    chatbot flow is skipped)."""
    tokens = [t.strip(".,!?:;") for t in text.strip().split()]
    lowers = {t.lower() for t in tokens}
    approve = bool(lowers & _APPROVE_WORDS)
    reject = bool(lowers & _REJECT_WORDS)
    if not (approve or reject):
        return False  # not a decision command — let the admin chat normally

    admin_number = settings.ADMIN_WHATSAPP_NUMBER

    # An optional booking reference token (e.g. "3F2A1B9C").
    action_words = _APPROVE_WORDS | _REJECT_WORDS
    ref = next(
        (t for t in tokens if t.lower() not in action_words and len(t) >= 4),
        None,
    )

    pending = db.query(Booking).filter(Booking.discount_status == "pending").all()
    if not pending:
        await send_whatsapp_text(admin_number, "No pending discount requests right now.")
        return True

    if ref:
        target = next((b for b in pending if b.id.lower().startswith(ref.lower())), None)
    elif len(pending) == 1:
        target = pending[0]
    else:
        refs = ", ".join(b.id[:8].upper() for b in pending)
        await send_whatsapp_text(
            admin_number,
            f"There are {len(pending)} pending discount requests. "
            f"Reply e.g. 'APPROVE {pending[0].id[:8].upper()}'.\nPending: {refs}",
        )
        return True

    if not target:
        await send_whatsapp_text(admin_number, f"No pending discount matches '{ref}'.")
        return True

    do_approve = approve and not reject
    tenant_msg = apply_discount_decision(db, target, do_approve)

    if target.guest_phone and target.guest_phone != "web":
        await send_whatsapp_text(target.guest_phone, tenant_msg)

    status = "APPROVED" if do_approve else "REJECTED"
    await send_whatsapp_text(
        admin_number,
        f"Discount {status} for booking {target.id[:8].upper()}. The tenant has been notified.",
    )
    return True


async def _handle_message(phone: str, text: str) -> None:
    """Run one inbound WhatsApp message through the agent and reply."""
    db = SessionLocal()
    try:
        admin_digits = _digits(settings.ADMIN_WHATSAPP_NUMBER)
        if admin_digits and _digits(phone) == admin_digits:
            if await _try_admin_command(text, db):
                return

        session_id = f"wa_{phone}"
        session = db.query(ChatbotSession).filter(ChatbotSession.id == session_id).first()
        if not session:
            session = ChatbotSession(id=session_id, phone=phone, state={})
            db.add(session)
            db.flush()

        db.add(ChatbotMessage(session_id=session.id, direction="inbound", message_text=text))

        state = dict(session.state or {})
        state["session_id"] = session.id
        state["phone"] = phone
        state["incoming_message"] = text

        result_state = await chatbot_graph.ainvoke(state)
        reply = result_state.get("reply", "Sorry, I didn't quite get that.")

        session.state = {k: v for k, v in result_state.items() if k not in ("incoming_message", "reply")}
        session.last_intent = result_state.get("intent")
        db.add(ChatbotMessage(session_id=session.id, direction="outbound", message_text=reply))
        db.commit()

        await send_whatsapp_text(phone, reply)
    except Exception:  # noqa: BLE001 - never let a bad message crash the worker
        logger.exception("Failed to handle WhatsApp message from %s", phone)
        db.rollback()
    finally:
        db.close()
