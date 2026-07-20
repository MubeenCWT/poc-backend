"""
Two-way WhatsApp chat via the Meta Cloud API.

  GET  /webhook/whatsapp  -> verification handshake (Meta calls this once)
  POST /webhook/whatsapp  -> incoming guest/admin messages (text or button taps)

Interactive reply buttons carry booking ids so admin never needs to type a full
reference — each open request gets its own Yes/No/Approve/Reject buttons.
"""
import logging
import re

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response

from app.chatbot.graph import chatbot_graph
from app.chatbot.owner_graph import handle_owner_message
from app.config import settings
from app.database import SessionLocal
from app.models.models import Booking, ChatbotMessage, ChatbotSession
from app.services.discounts import (
    apply_discount_decision,
    apply_discount_counter,
    apply_counter_response,
    admin_counter_result_alert,
)
from app.services.booking_confirm import (
    finalize_booking_confirmation,
    tenant_counter_buttons,
    tenant_full_price_buttons,
    admin_payment_buttons,
)
from app.services.notify import send_whatsapp_text, send_whatsapp_buttons, send_admin_alert
from app.services.owner_portfolio import find_owner_by_phone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["whatsapp"])

_APPROVE_WORDS = {"approve", "approved", "accept", "accepted"}
_REJECT_WORDS = {"reject", "rejected", "decline", "declined", "deny", "denied"}
_CONFIRM_WORDS = {"confirm", "confirmed", "paid", "payment", "yes"}
_OFFER_WORDS = {"offer", "counter", "bargain"}


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _admin_session(db, phone: str) -> ChatbotSession:
    sid = f"wa_{_digits(phone)}"
    session = db.query(ChatbotSession).filter(ChatbotSession.id == sid).first()
    if not session:
        session = ChatbotSession(id=sid, phone=phone, state={})
        db.add(session)
        db.flush()
    return session


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
                phone = msg.get("from")
                if not phone:
                    continue

                text = ""
                button_id = None
                msg_type = msg.get("type")

                if msg_type == "text":
                    text = (msg.get("text") or {}).get("body", "")
                elif msg_type == "interactive":
                    interactive = msg.get("interactive") or {}
                    if interactive.get("type") == "button_reply":
                        reply = interactive.get("button_reply") or {}
                        button_id = reply.get("id") or ""
                        text = reply.get("title") or button_id
                    elif interactive.get("type") == "list_reply":
                        reply = interactive.get("list_reply") or {}
                        button_id = reply.get("id") or ""
                        text = reply.get("title") or button_id

                if phone and (text or button_id):
                    background_tasks.add_task(_handle_message, phone, text, button_id)

    return {"status": "received"}


def _find_booking(db, booking_id: str | None = None, ref: str | None = None) -> Booking | None:
    if booking_id:
        return db.query(Booking).filter(Booking.id == booking_id).first()
    if ref:
        return next(
            (b for b in db.query(Booking).all() if b.id.lower().startswith(ref.lower())),
            None,
        )
    return None


async def _handle_button_id(button_id: str, db) -> bool:
    """Handle interactive button taps that encode booking actions. Returns True if handled."""
    admin_number = settings.ADMIN_WHATSAPP_NUMBER

    # pay_yes_<uuid> / pay_no_<uuid>
    m = re.match(r"^pay_(yes|no)_(.+)$", button_id)
    if m:
        action, bid = m.group(1), m.group(2)
        booking = _find_booking(db, booking_id=bid)
        if not booking:
            await send_whatsapp_text(admin_number, "That booking was not found.")
            return True
        if action == "no":
            await send_whatsapp_text(
                admin_number,
                f"Okay — booking {booking.id[:8].upper()} stays pending until payment arrives.",
            )
            return True
        if booking.status == "confirmed":
            await send_whatsapp_text(admin_number, f"Booking {booking.id[:8].upper()} is already confirmed.")
            return True
        tenant_msg = finalize_booking_confirmation(db, booking)
        if booking.guest_phone and booking.guest_phone != "web":
            await send_whatsapp_text(booking.guest_phone, tenant_msg)
        await send_whatsapp_text(
            admin_number,
            f"Booking {booking.id[:8].upper()} confirmed. Guest notified.",
        )
        return True

    # disc_ok_ / disc_no_ / disc_offer_
    m = re.match(r"^disc_(ok|no|offer)_(.+)$", button_id)
    if m:
        action, bid = m.group(1), m.group(2)
        booking = _find_booking(db, booking_id=bid)
        if not booking:
            await send_whatsapp_text(admin_number, "That booking was not found.")
            return True
        if booking.discount_status != "pending":
            await send_whatsapp_text(
                admin_number,
                f"Booking {booking.id[:8].upper()} is not awaiting a discount decision "
                f"(status: {booking.discount_status}).",
            )
            return True

        if action == "offer":
            session = _admin_session(db, admin_number)
            st = dict(session.state or {})
            st["pending_counter_booking_id"] = booking.id
            st["current_step"] = "wait_counter_amount"
            session.state = st
            db.commit()
            await send_whatsapp_text(
                admin_number,
                f"What discount (AED) do you want to offer for {booking.id[:8].upper()}?\n"
                f"Guest asked for AED {booking.discount_amount}. Base: AED {booking.base_price}.\n"
                f"Reply with a number only, e.g. 500",
            )
            return True

        tenant_msg = apply_discount_decision(db, booking, approve=(action == "ok"))
        buttons = None if action == "ok" else tenant_full_price_buttons(booking)
        if booking.guest_phone and booking.guest_phone != "web":
            if buttons:
                await send_whatsapp_buttons(booking.guest_phone, tenant_msg, buttons)
            else:
                await send_whatsapp_text(booking.guest_phone, tenant_msg)
        status = "APPROVED" if action == "ok" else "REJECTED"
        await send_whatsapp_text(
            admin_number,
            f"Discount {status} for {booking.id[:8].upper()}. Tenant notified.",
        )
        return True

    # Tenant: offer_yes_ / offer_no_
    m = re.match(r"^offer_(yes|no)_(.+)$", button_id)
    if m:
        action, bid = m.group(1), m.group(2)
        booking = _find_booking(db, booking_id=bid)
        if not booking:
            return False  # let chatbot try
        if booking.discount_status != "countered":
            await send_whatsapp_text(
                booking.guest_phone or "",
                "This offer is no longer available.",
            )
            return True
        accept = action == "yes"
        tenant_msg = apply_counter_response(db, booking, accept=accept)
        if booking.guest_phone and booking.guest_phone != "web":
            await send_whatsapp_text(booking.guest_phone, tenant_msg)

        from app.models.models import Property
        prop = db.query(Property).filter(Property.id == booking.property_id).first()
        admin_msg = admin_counter_result_alert(booking, accept, prop)
        await send_admin_alert(
            admin_msg,
            title="Booking Confirmed" if accept else "Booking Cancelled",
        )
        return True

    # Tenant: full_yes_ / full_no_
    m = re.match(r"^full_(yes|no)_(.+)$", button_id)
    if m:
        action, bid = m.group(1), m.group(2)
        booking = _find_booking(db, booking_id=bid)
        if not booking:
            return False
        phone = booking.guest_phone
        if action == "no":
            booking.status = "cancelled"
            from app.models.models import PropertyAvailability
            db.query(PropertyAvailability).filter(
                PropertyAvailability.booking_id == booking.id
            ).delete(synchronize_session=False)
            db.commit()
            if phone and phone != "web":
                await send_whatsapp_text(
                    phone,
                    "Okay — booking cancelled and dates released. Message us anytime to start again.",
                )
            return True

        # Accept full price → reserve + notify admin with payment buttons
        from app.services.booking_confirm import admin_payment_verification_message
        from app.models.models import Property, AdminNotification

        booking.final_price = booking.base_price
        prop = db.query(Property).filter(Property.id == booking.property_id).first()
        admin_msg = admin_payment_verification_message(booking, prop)
        db.add(AdminNotification(type="payment_pending", reference_id=booking.id, message=admin_msg))
        db.commit()
        if phone and phone != "web":
            await send_whatsapp_text(
                phone,
                f"Thanks! Your booking at full price is reserved.\n"
                f"Reference: {booking.id[:8].upper()}\n"
                f"Total: AED {booking.final_price}\n\n"
                f"We'll confirm once payment is received.",
            )
        if admin_number:
            await send_whatsapp_buttons(admin_number, f"*Booking Alert*\n\n{admin_msg}", admin_payment_buttons(booking))
        return True

    return False


async def _try_admin_command(text: str, db, phone: str) -> bool:
    """Handle admin WhatsApp text commands (and pending counter-amount entry)."""
    admin_number = settings.ADMIN_WHATSAPP_NUMBER
    session = _admin_session(db, phone)
    st = dict(session.state or {})

    # Waiting for counter-offer amount after tapping "Counter offer"
    if st.get("current_step") == "wait_counter_amount" and st.get("pending_counter_booking_id"):
        amount_match = re.search(r"[\d,.]+", text)
        if not amount_match:
            await send_whatsapp_text(admin_number, "Please reply with a number, e.g. 500")
            return True
        try:
            amount = float(amount_match.group(0).replace(",", ""))
        except ValueError:
            await send_whatsapp_text(admin_number, "Please reply with a valid amount in AED.")
            return True
        booking = _find_booking(db, booking_id=st["pending_counter_booking_id"])
        st.pop("pending_counter_booking_id", None)
        st["current_step"] = None
        session.state = st
        db.commit()
        if not booking:
            await send_whatsapp_text(admin_number, "Booking not found.")
            return True
        try:
            tenant_msg = apply_discount_counter(db, booking, amount)
        except ValueError as exc:
            await send_whatsapp_text(admin_number, str(exc))
            return True
        if booking.guest_phone and booking.guest_phone != "web":
            await send_whatsapp_buttons(
                booking.guest_phone, tenant_msg, tenant_counter_buttons(booking)
            )
        await send_whatsapp_text(
            admin_number,
            f"Counter-offer AED {amount:g} sent for {booking.id[:8].upper()}. Waiting on tenant.",
        )
        return True

    tokens = [t.strip(".,!?:;") for t in text.strip().split()]
    lowers = {t.lower() for t in tokens}
    approve = bool(lowers & _APPROVE_WORDS)
    reject = bool(lowers & _REJECT_WORDS)
    confirm = bool(lowers & _CONFIRM_WORDS)
    offer = bool(lowers & _OFFER_WORDS)

    action_words = _APPROVE_WORDS | _REJECT_WORDS | _CONFIRM_WORDS | _OFFER_WORDS
    ref = next(
        (t for t in tokens if t.lower() not in action_words and not re.fullmatch(r"[\d,.]+", t) and len(t) >= 4),
        None,
    )
    amount_token = next((t for t in tokens if re.fullmatch(r"[\d,.]+", t)), None)

    # OFFER [ref] 500  or  COUNTER 500 when only one pending
    if offer:
        pending = db.query(Booking).filter(Booking.discount_status == "pending").all()
        if not pending:
            await send_whatsapp_text(admin_number, "No pending discount requests.")
            return True
        if ref:
            target = next((b for b in pending if b.id.lower().startswith(ref.lower())), None)
        elif len(pending) == 1:
            target = pending[0]
        else:
            # Ask which one with buttons — Meta max 3, so take first 3
            lines = [f"There are {len(pending)} pending discounts. Tap Counter on the alert, or reply OFFER <ref> <amount>."]
            for b in pending[:5]:
                lines.append(f"  {b.id[:8].upper()} — asked AED {b.discount_amount}")
            await send_whatsapp_text(admin_number, "\n".join(lines))
            return True
        if not target:
            await send_whatsapp_text(admin_number, f"No pending discount matches '{ref}'.")
            return True
        if not amount_token:
            st["pending_counter_booking_id"] = target.id
            st["current_step"] = "wait_counter_amount"
            session.state = st
            db.commit()
            await send_whatsapp_text(
                admin_number,
                f"What amount (AED) for {target.id[:8].upper()}? Guest asked AED {target.discount_amount}.",
            )
            return True
        try:
            amount = float(amount_token.replace(",", ""))
            tenant_msg = apply_discount_counter(db, target, amount)
        except ValueError as exc:
            await send_whatsapp_text(admin_number, str(exc))
            return True
        if target.guest_phone and target.guest_phone != "web":
            await send_whatsapp_buttons(target.guest_phone, tenant_msg, tenant_counter_buttons(target))
        await send_whatsapp_text(
            admin_number,
            f"Counter-offer AED {amount:g} sent for {target.id[:8].upper()}.",
        )
        return True

    # Payment confirm — YES alone works when only one pending; otherwise pick with buttons
    if confirm and not (approve or reject):
        pending = (
            db.query(Booking)
            .filter(Booking.status == "pending", Booking.discount_status != "pending")
            .all()
        )
        if not pending:
            # "yes" alone might be casual — only claim it if it looks like a confirm command
            if text.strip().lower() in ("yes", "y", "ok", "confirm", "paid"):
                await send_whatsapp_text(admin_number, "No bookings awaiting payment confirmation.")
                return True
            return False

        if ref:
            target = next((b for b in pending if b.id.lower().startswith(ref.lower())), None)
        elif len(pending) == 1:
            target = pending[0]
        else:
            lines = [
                f"{len(pending)} bookings await payment. Tap *Yes, paid* on the alert for each, "
                f"or reply CONFIRM <ref>:"
            ]
            for b in pending[:8]:
                lines.append(f"  {b.id[:8].upper()} — {b.guest_name}")
            await send_whatsapp_text(admin_number, "\n".join(lines))
            return True

        if not target:
            await send_whatsapp_text(admin_number, f"No pending booking matches '{ref}'.")
            return True

        tenant_msg = finalize_booking_confirmation(db, target)
        if target.guest_phone and target.guest_phone != "web":
            await send_whatsapp_text(target.guest_phone, tenant_msg)
        await send_whatsapp_text(
            admin_number,
            f"Booking {target.id[:8].upper()} confirmed. Guest notified.",
        )
        return True

    if not (approve or reject):
        return False

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
            f"There are {len(pending)} pending discounts. "
            f"Use the buttons on each alert, or reply APPROVE/REJECT <ref>.\nPending: {refs}",
        )
        return True

    if not target:
        await send_whatsapp_text(admin_number, f"No pending discount matches '{ref}'.")
        return True

    do_approve = approve and not reject
    tenant_msg = apply_discount_decision(db, target, do_approve)
    if target.guest_phone and target.guest_phone != "web":
        if do_approve:
            await send_whatsapp_text(target.guest_phone, tenant_msg)
        else:
            await send_whatsapp_buttons(
                target.guest_phone, tenant_msg, tenant_full_price_buttons(target)
            )

    status = "APPROVED" if do_approve else "REJECTED"
    await send_whatsapp_text(
        admin_number,
        f"Discount {status} for booking {target.id[:8].upper()}. The tenant has been notified.",
    )
    return True


async def _try_owner_message(phone: str, text: str, db) -> bool:
    """Route registered property owners to the owner portfolio assistant."""
    owner = find_owner_by_phone(db, phone)
    if not owner:
        return False

    session_id = f"owner_wa_{_digits(phone)}"
    session = db.query(ChatbotSession).filter(ChatbotSession.id == session_id).first()
    if not session:
        session = ChatbotSession(id=session_id, phone=phone, state={"role": "owner"})
        db.add(session)
        db.flush()

    db.add(ChatbotMessage(session_id=session.id, direction="inbound", message_text=text))

    state = dict(session.state or {})
    state["owner_id"] = owner.id
    reply, new_state = await handle_owner_message(db, owner.id, text, state)

    session.state = new_state
    session.last_intent = "owner"
    db.add(ChatbotMessage(session_id=session.id, direction="outbound", message_text=reply))
    db.commit()

    await send_whatsapp_text(phone, reply)
    return True


async def _handle_message(phone: str, text: str, button_id: str | None = None) -> None:
    """Run one inbound WhatsApp message through admin commands or the agent."""
    db = SessionLocal()
    try:
        if button_id and await _handle_button_id(button_id, db):
            return

        admin_digits = _digits(settings.ADMIN_WHATSAPP_NUMBER)
        if admin_digits and _digits(phone) == admin_digits:
            if await _try_admin_command(text, db, phone):
                return

        if await _try_owner_message(phone, text, db):
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
        reply_buttons = result_state.get("reply_buttons")

        session.state = {
            k: v
            for k, v in result_state.items()
            if k not in ("incoming_message", "reply", "reply_buttons")
        }
        session.last_intent = result_state.get("intent")
        db.add(ChatbotMessage(session_id=session.id, direction="outbound", message_text=reply))
        db.commit()

        if reply_buttons:
            await send_whatsapp_buttons(phone, reply, reply_buttons)
        else:
            await send_whatsapp_text(phone, reply)
    except Exception:  # noqa: BLE001 - never let a bad message crash the worker
        logger.exception("Failed to handle WhatsApp message from %s", phone)
        db.rollback()
    finally:
        db.close()
