"""Shared booking confirmation helpers for the API and admin WhatsApp commands."""
from sqlalchemy.orm import Session

from app.models.models import Booking, Property, AdminNotification


def admin_payment_verification_message(booking: Booking, prop: Property | None) -> str:
    ref = booking.id[:8].upper()
    total = booking.final_price if booking.final_price is not None else booking.base_price
    title = prop.title if prop else booking.property_id
    return (
        f"Booking pending payment:\n"
        f"Property: {title}\n"
        f"Guest: {booking.guest_name} ({booking.guest_phone})\n"
        f"Dates: {booking.start_date} to {booking.end_date}\n"
        f"Total: AED {total}\n"
        f"Ref: {ref}\n\n"
        f"Have you received payment?"
    )


def admin_payment_buttons(booking: Booking) -> list[dict]:
    return [
        {"id": f"pay_yes_{booking.id}", "title": "Yes, paid"},
        {"id": f"pay_no_{booking.id}", "title": "Not yet"},
    ]


def admin_discount_buttons(booking: Booking) -> list[dict]:
    return [
        {"id": f"disc_ok_{booking.id}", "title": "Approve"},
        {"id": f"disc_no_{booking.id}", "title": "Reject"},
        {"id": f"disc_offer_{booking.id}", "title": "Counter offer"},
    ]


def tenant_full_price_buttons(booking: Booking) -> list[dict]:
    return [
        {"id": f"full_yes_{booking.id}", "title": "Yes, full price"},
        {"id": f"full_no_{booking.id}", "title": "No thanks"},
    ]


def tenant_counter_buttons(booking: Booking) -> list[dict]:
    return [
        {"id": f"offer_yes_{booking.id}", "title": "Accept offer"},
        {"id": f"offer_no_{booking.id}", "title": "Decline"},
    ]


def tenant_pending_payment_message(booking: Booking, prop: Property | None) -> str:
    ref = booking.id[:8].upper()
    total = booking.final_price if booking.final_price is not None else booking.base_price
    title = prop.title if prop else "your stay"
    return (
        f"Thanks, {booking.guest_name}!\n"
        f"Your booking for {title} is reserved.\n"
        f"Reference: {ref}\n"
        f"Dates: {booking.start_date} to {booking.end_date}\n"
        f"Total: AED {total}\n\n"
        f"We'll confirm your booking once payment is received."
    )


def tenant_confirmed_message(booking: Booking, prop: Property | None) -> str:
    ref = booking.id[:8].upper()
    title = prop.title if prop else "your stay"
    return (
        f"Your booking is confirmed!\n"
        f"Reference: {ref}\n"
        f"Property: {title}\n"
        f"Dates: {booking.start_date} to {booking.end_date}\n"
        f"Total: AED {booking.final_price}\n\n"
        f"See you soon!"
    )


def finalize_booking_confirmation(db: Session, booking: Booking) -> str:
    """Mark booking confirmed and return the message to send to the tenant."""
    if booking.status == "confirmed":
        prop = db.query(Property).filter(Property.id == booking.property_id).first()
        return tenant_confirmed_message(booking, prop)

    booking.status = "confirmed"
    if booking.final_price is None:
        booking.final_price = booking.base_price

    prop = db.query(Property).filter(Property.id == booking.property_id).first()
    log = (
        f"Booking confirmed:\n"
        f"Property: {prop.title if prop else booking.property_id}\n"
        f"Guest: {booking.guest_name}\n"
        f"Dates: {booking.start_date} to {booking.end_date}\n"
        f"Total: AED {booking.final_price}"
    )
    db.add(AdminNotification(type="booking_confirmed", reference_id=booking.id, message=log))
    db.commit()
    db.refresh(booking)
    return tenant_confirmed_message(booking, prop)
