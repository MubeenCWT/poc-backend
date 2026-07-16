from typing import List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.database import get_db
from app.models.models import Booking, Property, PropertyAvailability, DiscountRequest, User, AdminNotification
from app.schemas.schemas import BookingCreate, BookingOut, DiscountDecision, CounterResponse
from app.services.deps import get_current_admin
from app.services.notify import send_admin_alert, send_whatsapp_text, send_whatsapp_buttons
from app.services.discounts import apply_discount_decision, apply_discount_counter, apply_counter_response, admin_counter_result_alert
from app.services.booking_confirm import (
    admin_payment_verification_message,
    admin_payment_buttons,
    admin_discount_buttons,
    tenant_full_price_buttons,
    tenant_counter_buttons,
    finalize_booking_confirmation,
)

router = APIRouter(prefix="/bookings", tags=["bookings"])


def _months_between(start_date, end_date) -> int:
    return (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)


def _compute_base_price(prop: Property, booking_type: str, start_date, end_date) -> float:
    """Duration-aware price so bills reflect the actual length of stay."""
    if booking_type == "daily":
        nights = max((end_date - start_date).days, 1)
        return float(prop.price_daily or 0) * nights
    if booking_type == "monthly":
        months = max(_months_between(start_date, end_date), 1)
        return float(prop.price_monthly or 0) * months
    if booking_type == "yearly":
        years = max(round(_months_between(start_date, end_date) / 12) or 1, 1)
        return float(prop.price_yearly or 0) * years
    raise HTTPException(400, "Invalid booking_type")


def _has_conflict(db: Session, property_id: str, start_date, end_date) -> bool:
    overlap = db.query(PropertyAvailability).filter(
        PropertyAvailability.property_id == property_id,
        PropertyAvailability.status.in_(["booked", "blocked"]),
        and_(
            PropertyAvailability.start_date <= end_date,
            PropertyAvailability.end_date >= start_date,
        ),
    ).first()
    return overlap is not None


async def _push_alert(message: str, buttons=None, title: str = "Booking Alert"):
    await send_admin_alert(message, title=title, buttons=buttons)


async def _push_text(to: str, message: str, buttons=None):
    if buttons:
        await send_whatsapp_buttons(to, message, buttons)
    else:
        await send_whatsapp_text(to, message)


@router.get("/check-availability")
def check_availability(property_id: str, start_date: str, end_date: str, db: Session = Depends(get_db)):
    if _has_conflict(db, property_id, start_date, end_date):
        return {"available": False}
    return {"available": True}


@router.post("/", response_model=BookingOut)
async def create_booking(
    payload: BookingCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    guest_name = " ".join((payload.guest_name or "").strip().split())
    if len(guest_name) < 2 or not any(c.isalpha() for c in guest_name):
        raise HTTPException(status_code=400, detail="A valid guest full name is required before booking.")

    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="end_date cannot be before start_date")

    prop = db.query(Property).filter(Property.id == payload.property_id).first()
    if not prop:
        raise HTTPException(404, "Property not found")

    if _has_conflict(db, payload.property_id, payload.start_date, payload.end_date):
        raise HTTPException(409, "Property is not available for the selected dates")

    base_price = _compute_base_price(prop, payload.booking_type, payload.start_date, payload.end_date)

    booking = Booking(
        property_id=payload.property_id,
        guest_name=guest_name,
        guest_phone=payload.guest_phone,
        booking_type=payload.booking_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        base_price=base_price,
        discount_requested=payload.discount_requested,
        discount_amount=payload.discount_amount or 0,
        discount_status="pending" if payload.discount_requested else "none",
        final_price=base_price if not payload.discount_requested else None,
        status="pending",
        source=payload.source,
    )
    db.add(booking)
    db.flush()

    availability = PropertyAvailability(
        property_id=payload.property_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        status="booked",
        booking_id=booking.id,
    )
    db.add(availability)

    if payload.discount_requested:
        discount_req = DiscountRequest(
            booking_id=booking.id,
            requested_amount=payload.discount_amount,
            reason="Requested via chatbot at time of booking",
        )
        db.add(discount_req)

    if payload.discount_requested:
        ref = booking.id[:8].upper()
        notification_text = (
            f"New booking with discount request:\n"
            f"Property: {prop.title}\n"
            f"Guest: {payload.guest_name} ({payload.guest_phone})\n"
            f"Dates: {payload.start_date} to {payload.end_date}\n"
            f"Type: {payload.booking_type}, Base price: AED {base_price}\n"
            f"Discount requested: AED {payload.discount_amount}.\n"
            f"Ref: {ref}\n\n"
            f"Tap a button, or reply APPROVE / REJECT / OFFER {ref} <amount>"
        )
        notif_type = "new_booking"
        buttons = admin_discount_buttons(booking)
    else:
        notification_text = admin_payment_verification_message(booking, prop)
        notif_type = "payment_pending"
        buttons = admin_payment_buttons(booking)

    db.add(AdminNotification(type=notif_type, reference_id=booking.id, message=notification_text))
    db.commit()
    db.refresh(booking)

    background_tasks.add_task(_push_alert, notification_text, buttons)

    return booking


@router.get("/", response_model=List[BookingOut])
def list_bookings(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    return db.query(Booking).order_by(Booking.created_at.desc()).all()


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(booking_id: str, db: Session = Depends(get_db)):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    return booking


@router.post("/{booking_id}/confirm", response_model=BookingOut)
async def confirm_booking(
    booking_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking.status == "confirmed":
        return booking

    tenant_msg = finalize_booking_confirmation(db, booking)

    if booking.guest_phone and booking.guest_phone != "web":
        background_tasks.add_task(_push_text, booking.guest_phone, tenant_msg)

    return booking


@router.post("/{booking_id}/guest-confirm", response_model=BookingOut)
async def guest_confirm_booking(
    booking_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Guest accepts full price after a rejected discount — awaits admin payment confirmation."""
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking.status == "confirmed":
        return booking
    if booking.status != "pending":
        raise HTTPException(400, detail="Booking cannot be confirmed in its current state")

    booking.final_price = booking.base_price
    prop = db.query(Property).filter(Property.id == booking.property_id).first()

    admin_msg = admin_payment_verification_message(booking, prop)
    db.add(AdminNotification(type="payment_pending", reference_id=booking.id, message=admin_msg))
    db.commit()
    db.refresh(booking)

    background_tasks.add_task(_push_alert, admin_msg, admin_payment_buttons(booking))
    return booking


@router.post("/{booking_id}/discount-decision", response_model=BookingOut)
async def decide_discount(
    booking_id: str,
    decision: DiscountDecision,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking.discount_status != "pending":
        raise HTTPException(400, "No pending discount decision for this booking")

    buttons = None
    if decision.counter_amount is not None:
        try:
            tenant_msg = apply_discount_counter(
                db, booking, float(decision.counter_amount), decided_by=admin.id
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        notif_type = "discount_countered"
        log = (
            f"Counter-offer AED {decision.counter_amount} sent for booking "
            f"{booking.id[:8].upper()} — waiting on tenant."
        )
        buttons = tenant_counter_buttons(booking)
    else:
        tenant_msg = apply_discount_decision(db, booking, decision.approve, decided_by=admin.id)
        notif_type = "discount_approved" if decision.approve else "discount_rejected"
        log = (
            f"Discount {'approved' if decision.approve else 'rejected'} for booking "
            f"{booking.id[:8].upper()} — tenant notified."
        )
        if not decision.approve:
            buttons = tenant_full_price_buttons(booking)

    db.add(AdminNotification(type=notif_type, reference_id=booking.id, message=log))
    db.commit()
    db.refresh(booking)

    if booking.guest_phone and booking.guest_phone != "web":
        background_tasks.add_task(_push_text, booking.guest_phone, tenant_msg, buttons)

    return booking


@router.post("/{booking_id}/counter-response", response_model=BookingOut)
async def respond_to_counter(
    booking_id: str,
    payload: CounterResponse,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Tenant accepts or declines an admin counter-offer."""
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    if booking.discount_status != "countered":
        raise HTTPException(400, "No counter-offer awaiting a response")

    apply_counter_response(db, booking, payload.accept)
    prop = db.query(Property).filter(Property.id == booking.property_id).first()
    admin_msg = admin_counter_result_alert(booking, payload.accept, prop)
    title = "Booking Confirmed" if payload.accept else "Booking Cancelled"
    background_tasks.add_task(_push_alert, admin_msg, None, title)

    return booking
