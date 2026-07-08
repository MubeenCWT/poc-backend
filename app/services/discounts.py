"""
Shared discount-decision logic used by both the admin web portal and the
two-way WhatsApp webhook (admin can approve/reject straight from WhatsApp).

`apply_discount_decision` mutates the DB (booking + discount request), and on a
rejection it primes the tenant's chatbot session so a plain "yes" reply confirms
the booking at full price. It returns the message that should be sent to the
TENANT — the caller is responsible for actually delivering it over WhatsApp.
"""
import datetime

from sqlalchemy.orm import Session

from app.models.models import Booking, DiscountRequest, Property, ChatbotSession


def _tenant_session_id(booking: Booking) -> str | None:
    phone = "".join(ch for ch in (booking.guest_phone or "") if ch.isdigit())
    return f"wa_{phone}" if phone else None


def apply_discount_decision(
    db: Session,
    booking: Booking,
    approve: bool,
    decided_by: str | None = None,
) -> str:
    """Apply an approve/reject decision to a booking's discount request.

    Returns the message to send to the tenant.
    """
    discount_req = (
        db.query(DiscountRequest)
        .filter(DiscountRequest.booking_id == booking.id)
        .first()
    )
    prop = db.query(Property).filter(Property.id == booking.property_id).first()
    title = prop.title if prop else booking.property_id
    ref = booking.id[:8].upper()

    if approve:
        booking.discount_status = "approved"
        booking.final_price = float(booking.base_price) - float(booking.discount_amount or 0)
        booking.status = "confirmed"
        if discount_req:
            discount_req.status = "approved"
        tenant_msg = (
            f"Great news! Your discount on {title} was approved.\n"
            f"Booking {ref} is now CONFIRMED.\n"
            f"Dates: {booking.start_date} to {booking.end_date}\n"
            f"Total: AED {booking.final_price} "
            f"(you saved AED {booking.discount_amount}).\n"
            f"Please proceed with payment to complete your stay."
        )
    else:
        booking.discount_status = "rejected"
        booking.final_price = booking.base_price
        if discount_req:
            discount_req.status = "rejected"
        tenant_msg = (
            f"Update on your booking {ref} for {title}:\n"
            f"Unfortunately the requested discount could not be approved.\n"
            f"The full price is AED {booking.base_price}.\n"
            f"Reply 'yes' to confirm at full price and keep your dates reserved."
        )
        # Prime the tenant's WhatsApp session so a plain "yes" confirms at full price.
        session_id = _tenant_session_id(booking)
        if session_id:
            session = (
                db.query(ChatbotSession)
                .filter(ChatbotSession.id == session_id)
                .first()
            )
            if session:
                new_state = dict(session.state or {})
                new_state["booking_id"] = booking.id
                new_state["current_step"] = "wait_accept_full_price"
                new_state["intent"] = "discount_check"
                session.state = new_state
                session.last_intent = "discount_check"

    if discount_req:
        discount_req.decided_by = decided_by
        discount_req.decided_at = datetime.datetime.utcnow()

    db.commit()
    db.refresh(booking)
    return tenant_msg
