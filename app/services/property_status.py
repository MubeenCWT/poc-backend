"""Property listing status helpers — blocks, tags for API + UI."""
import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.models import Property, PropertyAvailability


def _today() -> datetime.date:
    return datetime.date.today()


def get_current_or_next_block(
    db: Session, property_id: str, as_of: Optional[datetime.date] = None
) -> Optional[PropertyAvailability]:
    day = as_of or _today()
    return (
        db.query(PropertyAvailability)
        .filter(
            PropertyAvailability.property_id == property_id,
            PropertyAvailability.status == "blocked",
            PropertyAvailability.end_date >= day,
        )
        .order_by(PropertyAvailability.start_date)
        .first()
    )


def is_blocked_on(db: Session, property_id: str, day: Optional[datetime.date] = None) -> bool:
    day = day or _today()
    row = (
        db.query(PropertyAvailability)
        .filter(
            PropertyAvailability.property_id == property_id,
            PropertyAvailability.status == "blocked",
            PropertyAvailability.start_date <= day,
            PropertyAvailability.end_date >= day,
        )
        .first()
    )
    return row is not None


def property_block_fields(db: Session, prop: Property) -> dict:
    """Extra fields for PropertyOut — block tags on website + admin."""
    today = _today()
    block = get_current_or_next_block(db, prop.id, today)
    active_today = is_blocked_on(db, prop.id, today)
    if not block:
        return {
            "block_active": False,
            "block_start": None,
            "block_end": None,
            "listing_label": "live" if prop.status == "active" else "removed",
        }
    label = "blocked" if active_today else "blocked_soon"
    if prop.status != "active":
        label = "removed"
    return {
        "block_active": active_today,
        "block_start": block.start_date,
        "block_end": block.end_date,
        "listing_label": label,
    }


def serialize_property(db: Session, prop: Property) -> dict:
    data = {
        "id": prop.id,
        "owner_id": prop.owner_id,
        "title": prop.title,
        "description": prop.description,
        "property_type": prop.property_type,
        "emirate": prop.emirate,
        "area": prop.area,
        "address": prop.address,
        "bedrooms": prop.bedrooms,
        "bathrooms": prop.bathrooms,
        "max_guests": prop.max_guests,
        "amenities": prop.amenities or [],
        "images": prop.images or [],
        "price_daily": float(prop.price_daily) if prop.price_daily is not None else None,
        "price_monthly": float(prop.price_monthly) if prop.price_monthly is not None else None,
        "price_yearly": float(prop.price_yearly) if prop.price_yearly is not None else None,
        "status": prop.status,
        "created_at": prop.created_at,
    }
    data.update(property_block_fields(db, prop))
    return data
