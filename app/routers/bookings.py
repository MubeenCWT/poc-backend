from typing import List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.database import get_db
from app.models.models import Booking, Property, PropertyAvailability, DiscountRequest, User, AdminNotification
from app.schemas.schemas import BookingCreate, BookingOut, DiscountDecision
from app.services.deps import get_current_admin
#from app.services.whatsapp import notify_admin

router = APIRouter(prefix="/bookings", tags=["bookings"])


def _price_for_type(prop: Property, booking_type: str) -> float:
    return {
        "daily": prop.price_daily,
        "monthly": prop.price_monthly,
        "yearly": prop.price_yearly,
    }[booking_type]


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


@router.get("/check-availability")
def check_availability(property_id: str, start_date: str, end_date: str, db: Session = Depends(get_db)):
    if _has_conflict(db, property_id, start_date, end_date):
        return {"available": False}
    return {"available": True}

@router.post("/", response_model=BookingOut)
def create_booking(payload: BookingCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="end_date cannot be before start_date")

    prop = db.query(Property).filter(Property.id == payload.property_id).first()
    if not prop:
        raise HTTPException(404, "Property not found")

    if _has_conflict(db, payload.property_id, payload.start_date, payload.end_date):
        raise HTTPException(409, "Property is not available for the selected dates")

    base_price = _price_for_type(prop, payload.booking_type)

    booking = Booking(
        property_id=payload.property_id,
        guest_name=payload.guest_name,
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
    db.flush()  # get booking.id before commit

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

    notification_text = (
        f"New booking request:\n"
        f"Property: {prop.title}\n"
        f"Guest: {payload.guest_name} ({payload.guest_phone})\n"
        f"Dates: {payload.start_date} to {payload.end_date}\n"
        f"Type: {payload.booking_type}, Base price: {base_price}"
    )
    if payload.discount_requested:
        notification_text += f"\nDiscount requested: {payload.discount_amount} — needs your approval."

    db.add(AdminNotification(type="new_booking", reference_id=booking.id, message=notification_text))
    db.commit()
    db.refresh(booking)

    return booking


@router.get("/", response_model=List[BookingOut])
def list_bookings(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    """Admin-only: view all bookings in the portal."""
    return db.query(Booking).order_by(Booking.created_at.desc()).all()


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(booking_id: str, db: Session = Depends(get_db)):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    if not booking:
        raise HTTPException(404, "Booking not found")
    return booking


@router.post("/{booking_id}/confirm", response_model=BookingOut)
def confirm_booking(booking_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    booking.status = "confirmed"
    db.commit()
    db.refresh(booking)
    return booking


@router.post("/{booking_id}/discount-decision", response_model=BookingOut)
def decide_discount(
    booking_id: str,
    decision: DiscountDecision,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admin approves or rejects a discount request from the portal."""
    booking = db.query(Booking).filter(Booking.id == booking_id).first()
    discount_req = db.query(DiscountRequest).filter(DiscountRequest.booking_id == booking_id).first()

    if decision.approve:
        booking.discount_status = "approved"
        booking.final_price = float(booking.base_price) - float(booking.discount_amount)
        if discount_req:
            discount_req.status = "approved"
    else:
        booking.discount_status = "rejected"
        booking.final_price = booking.base_price
        if discount_req:
            discount_req.status = "rejected"

    if discount_req:
        discount_req.decided_by = admin.id

    db.commit()
    db.refresh(booking)
    return booking
