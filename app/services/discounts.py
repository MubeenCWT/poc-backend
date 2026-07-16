"""
Shared discount-decision logic used by both the admin web portal and the
two-way WhatsApp webhook.

Supports:
  - approve / reject the requested discount
  - counter-offer a different amount (tenant must accept or decline)
"""
import datetime

from sqlalchemy.orm import Session

from app.models.models import Booking, DiscountRequest, Property, ChatbotSession


def _tenant_session_id(booking: Booking) -> str | None:
    phone = "".join(ch for ch in (booking.guest_phone or "") if ch.isdigit())
    return f"wa_{phone}" if phone else None


def _prime_tenant_session(db: Session, booking: Booking, step: str, extra: dict | None = None) -> None:
    session_id = _tenant_session_id(booking)
    if not session_id:
        return
    session = db.query(ChatbotSession).filter(ChatbotSession.id == session_id).first()
    if not session:
        session = ChatbotSession(id=session_id, phone=booking.guest_phone, state={})
        db.add(session)
        db.flush()
    new_state = dict(session.state or {})
    new_state["booking_id"] = booking.id
    new_state["current_step"] = step
    new_state["intent"] = "discount_check"
    if extra:
        new_state.update(extra)
    session.state = new_state
    session.last_intent = "discount_check"


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
            f"Would you like to continue at full price?"
        )
        _prime_tenant_session(db, booking, "wait_accept_full_price")

    if discount_req:
        discount_req.decided_by = decided_by
        discount_req.decided_at = datetime.datetime.utcnow()

    db.commit()
    db.refresh(booking)
    return tenant_msg


def apply_discount_counter(
    db: Session,
    booking: Booking,
    counter_amount: float,
    decided_by: str | None = None,
) -> str:
    """Admin offers a different discount. Tenant must accept or decline.

    Returns the message to send to the tenant.
    """
    if counter_amount <= 0:
        raise ValueError("Counter amount must be greater than 0")
    if counter_amount >= float(booking.base_price):
        raise ValueError("Counter amount must be less than the base price")

    discount_req = (
        db.query(DiscountRequest)
        .filter(DiscountRequest.booking_id == booking.id)
        .first()
    )
    prop = db.query(Property).filter(Property.id == booking.property_id).first()
    title = prop.title if prop else booking.property_id
    ref = booking.id[:8].upper()
    new_total = float(booking.base_price) - float(counter_amount)

    booking.discount_status = "countered"
    if discount_req:
        discount_req.status = "countered"
        discount_req.counter_amount = counter_amount
        discount_req.decided_by = decided_by
        discount_req.decided_at = datetime.datetime.utcnow()

    _prime_tenant_session(
        db,
        booking,
        "wait_counter_response",
        {"counter_amount": float(counter_amount)},
    )

    db.commit()
    db.refresh(booking)

    return (
        f"Update on your booking {ref} for {title}:\n"
        f"You asked for AED {booking.discount_amount} off.\n"
        f"We can offer AED {counter_amount:g} off instead "
        f"(new total: AED {new_total:g}).\n\n"
        f"Would you like to accept this offer?"
    )


def apply_counter_response(
    db: Session,
    booking: Booking,
    accept: bool,
) -> str:
    """Tenant accepts or declines an admin counter-offer.

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
    counter = float(discount_req.counter_amount) if discount_req and discount_req.counter_amount else 0

    if accept:
        booking.discount_amount = counter
        booking.discount_status = "approved"
        booking.final_price = float(booking.base_price) - counter
        booking.status = "confirmed"
        if discount_req:
            discount_req.status = "approved"
            discount_req.decided_at = datetime.datetime.utcnow()
        from app.models.models import AdminNotification
        db.add(AdminNotification(
            type="booking_confirmed",
            reference_id=booking.id,
            message=(
                f"Booking confirmed (counter-offer accepted):\n"
                f"Property: {title}\n"
                f"Guest: {booking.guest_name} ({booking.guest_phone})\n"
                f"Dates: {booking.start_date} to {booking.end_date}\n"
                f"Total: AED {booking.final_price} "
                f"(AED {counter:g} off base AED {booking.base_price})\n"
                f"Ref: {ref}"
            ),
        ))
        msg = (
            f"Perfect — offer accepted!\n"
            f"Booking {ref} for {title} is CONFIRMED.\n"
            f"Dates: {booking.start_date} to {booking.end_date}\n"
            f"Total: AED {booking.final_price} (AED {counter:g} off).\n"
            f"Please proceed with payment to complete your stay."
        )
    else:
        booking.discount_status = "rejected"
        booking.final_price = None
        booking.status = "cancelled"
        if discount_req:
            discount_req.status = "rejected"
            discount_req.decided_at = datetime.datetime.utcnow()
        from app.models.models import PropertyAvailability, AdminNotification
        db.query(PropertyAvailability).filter(
            PropertyAvailability.booking_id == booking.id
        ).delete(synchronize_session=False)
        db.add(AdminNotification(
            type="booking_cancelled",
            reference_id=booking.id,
            message=(
                f"Tenant declined counter-offer — booking {ref} cancelled.\n"
                f"Property: {title}\n"
                f"Guest: {booking.guest_name}"
            ),
        ))
        msg = (
            f"No problem — we've cancelled booking {ref} for {title}.\n"
            f"Your dates are released. Message us anytime to book again."
        )

    db.commit()
    db.refresh(booking)
    return msg


def admin_counter_result_alert(booking: Booking, accept: bool, prop: Property | None = None) -> str:
    """Full alert text for admin after tenant accepts/declines a counter-offer."""
    title = prop.title if prop else booking.property_id
    ref = booking.id[:8].upper()
    if accept:
        return (
            f"Booking confirmed — tenant accepted your counter-offer.\n"
            f"Property: {title}\n"
            f"Guest: {booking.guest_name} ({booking.guest_phone})\n"
            f"Dates: {booking.start_date} to {booking.end_date}\n"
            f"Total: AED {booking.final_price}\n"
            f"(AED {booking.discount_amount} off base AED {booking.base_price})\n"
            f"Ref: {ref}"
        )
    return (
        f"Tenant declined your counter-offer — booking cancelled.\n"
        f"Property: {title}\n"
        f"Guest: {booking.guest_name}\n"
        f"Ref: {ref}"
    )
