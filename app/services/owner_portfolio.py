"""Portfolio queries and availability management for property owners."""
import datetime
from typing import Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.models import Booking, Property, PropertyAvailability


def _digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def find_owner_by_phone(db: Session, phone: str) -> Optional[object]:
    from app.models.models import User

    digits = _digits(phone)
    if not digits:
        return None
    for user in db.query(User).filter(User.role == "owner", User.is_active == True).all():  # noqa: E712
        if user.phone and _digits(user.phone) == digits:
            return user
    return None


def owner_properties(db: Session, owner_id: str, active_only: bool = False) -> list[Property]:
    q = db.query(Property).filter(Property.owner_id == owner_id)
    if active_only:
        q = q.filter(Property.status == "active")
    return q.order_by(Property.title).all()


def match_owner_property(db: Session, owner_id: str, query: str) -> Optional[Property]:
    q = query.lower().strip()
    if not q or q == "unknown":
        return None
    for prop in owner_properties(db, owner_id):
        title = (prop.title or "").lower()
        area = (prop.area or "").lower()
        if q in title or title in q or (area and (q in area or area in q)):
            return prop
    return None


def _is_occupied_on(db: Session, prop: Property, day: datetime.date) -> bool:
    if prop.status != "active":
        return True  # offline = not available for rent
    row = (
      db.query(PropertyAvailability)
      .filter(
          PropertyAvailability.property_id == prop.id,
          PropertyAvailability.status.in_(["booked", "blocked"]),
          PropertyAvailability.start_date <= day,
          PropertyAvailability.end_date >= day,
      )
      .first()
  )
  return row is not None


def portfolio_summary(db: Session, owner_id: str, as_of: Optional[datetime.date] = None) -> dict:
    """How many units are vacant vs occupied today."""
    day = as_of or datetime.date.today()
    props = owner_properties(db, owner_id)
    vacant = []
    occupied = []
    offline = []
    for prop in props:
        if prop.status != "active":
            offline.append(prop)
        elif _is_occupied_on(db, prop, day):
            occupied.append(prop)
        else:
            vacant.append(prop)
    return {
        "as_of": day,
        "total": len(props),
        "vacant": vacant,
        "occupied": occupied,
        "offline": offline,
    }


def next_release_info(db: Session, prop: Property, from_date: Optional[datetime.date] = None) -> dict:
    """When the property becomes free after current bookings/blocks."""
    day = from_date or datetime.date.today()
    future = (
        db.query(PropertyAvailability)
        .filter(
            PropertyAvailability.property_id == prop.id,
            PropertyAvailability.status.in_(["booked", "blocked"]),
            PropertyAvailability.end_date >= day,
        )
        .order_by(PropertyAvailability.end_date.desc())
        .all()
    )
    if not future:
        if prop.status != "active":
            return {"status": "offline", "available_from": None, "message": "Property is offline from listings."}
        if _is_occupied_on(db, prop, day):
            return {"status": "occupied", "available_from": None, "message": "Occupied but no end date on file."}
        return {"status": "available", "available_from": day, "message": "Available now — no active booking or block."}

    latest_end = max(r.end_date for r in future)
    # Check if currently occupied
    currently_busy = any(r.start_date <= day <= r.end_date for r in future)
    if not currently_busy and prop.status == "active":
        return {"status": "available", "available_from": day, "message": "Available now."}

    release = latest_end + datetime.timedelta(days=1)
    booking = (
        db.query(Booking)
        .filter(
            Booking.property_id == prop.id,
            Booking.status.in_(["pending", "confirmed"]),
            Booking.end_date == latest_end,
        )
        .order_by(Booking.end_date.desc())
        .first()
    )
    return {
        "status": "occupied",
        "available_from": release,
        "guest": booking.guest_name if booking else None,
        "booking_end": latest_end,
    }


def block_property_dates(
    db: Session,
    prop: Property,
    start_date: datetime.date,
    end_date: datetime.date,
) -> PropertyAvailability:
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")
    overlap = (
        db.query(PropertyAvailability)
        .filter(
            PropertyAvailability.property_id == prop.id,
            PropertyAvailability.status.in_(["booked", "blocked"]),
            PropertyAvailability.start_date <= end_date,
            PropertyAvailability.end_date >= start_date,
        )
        .first()
    )
    if overlap:
        raise ValueError(
            f"Dates overlap an existing {overlap.status} period "
            f"({overlap.start_date} to {overlap.end_date})."
        )
    row = PropertyAvailability(
        property_id=prop.id,
        start_date=start_date,
        end_date=end_date,
        status="blocked",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def take_property_offline(
    db: Session,
    prop: Property,
    start_date: datetime.date,
    end_date: datetime.date,
) -> None:
    """Hide from listings and block bookings for a date range."""
    block_property_dates(db, prop, start_date, end_date)
    prop.status = "inactive"
    db.commit()
    db.refresh(prop)


def bring_property_online(db: Session, prop: Property) -> None:
    prop.status = "active"
    db.commit()
    db.refresh(prop)
