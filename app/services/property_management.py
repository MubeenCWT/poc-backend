"""Business logic for admin property-management actions."""

import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.models import Property, PropertyAvailability


def get_property_or_404(db: Session, property_id: str) -> Property:
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    return prop


def block_dates(
    db: Session,
    prop: Property,
    start_date: datetime.date,
    end_date: datetime.date,
) -> Property:
    if end_date < start_date:
        raise HTTPException(
            status_code=400,
            detail="End date must be on or after start date",
        )

    conflict = (
        db.query(PropertyAvailability)
        .filter(
            PropertyAvailability.property_id == prop.id,
            PropertyAvailability.status.in_(["booked", "blocked"]),
            PropertyAvailability.start_date <= end_date,
            PropertyAvailability.end_date >= start_date,
        )
        .first()
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Dates overlap an existing {conflict.status} period "
                f"({conflict.start_date} to {conflict.end_date})"
            ),
        )

    db.add(
        PropertyAvailability(
            property_id=prop.id,
            start_date=start_date,
            end_date=end_date,
            status="blocked",
        )
    )
    db.commit()
    db.refresh(prop)
    return prop


def clear_blocks(db: Session, prop: Property) -> Property:
    db.query(PropertyAvailability).filter(
        PropertyAvailability.property_id == prop.id,
        PropertyAvailability.status == "blocked",
    ).delete(synchronize_session=False)
    db.commit()
    db.refresh(prop)
    return prop


def set_listing_status(db: Session, prop: Property, status: str) -> Property:
    if status not in ("active", "offline", "inactive"):
        raise HTTPException(status_code=400, detail="Invalid property status")
    prop.status = status
    db.commit()
    db.refresh(prop)
    return prop

