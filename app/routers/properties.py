from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Property, PropertyAvailability, User
from app.schemas.schemas import PropertyBlockRequest, PropertyCreate, PropertyOut
from app.services.deps import get_current_admin, get_optional_admin
from app.services.property_status import serialize_property

router = APIRouter(prefix="/properties", tags=["properties"])


def _property_out(db: Session, prop: Property) -> PropertyOut:
    return PropertyOut(**serialize_property(db, prop))


@router.get("/", response_model=List[PropertyOut])
def list_properties(
    emirate: Optional[str] = None,
    area: Optional[str] = None,
    property_type: Optional[str] = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    admin: Optional[User] = Depends(get_optional_admin),
):
    """Public listings by default. Admins may pass include_inactive=true to see all."""
    if include_inactive:
        if admin is None:
            raise HTTPException(status_code=401, detail="Admin authentication required")
        props = db.query(Property).order_by(Property.created_at.desc()).all()
        return [_property_out(db, p) for p in props]

    query = db.query(Property).filter(Property.status == "active")
    if emirate:
        query = query.filter(Property.emirate == emirate)
    if area:
        query = query.filter(Property.area == area)
    if property_type:
        query = query.filter(Property.property_type == property_type)
    return [_property_out(db, p) for p in query.all()]


@router.get("/admin/all", response_model=List[PropertyOut])
def list_properties_admin(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admin-only alias — prefer GET /?include_inactive=true on older deployments."""
    props = db.query(Property).order_by(Property.created_at.desc()).all()
    return [_property_out(db, p) for p in props]


@router.get("/{property_id}", response_model=PropertyOut)
def get_property(property_id: str, db: Session = Depends(get_db)):
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    return _property_out(db, prop)


@router.post("/", response_model=PropertyOut)
def create_property(
    payload: PropertyCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admin-only: add a new property from the portal."""
    prop = Property(owner_id=admin.id, **payload.model_dump())
    db.add(prop)
    db.commit()
    db.refresh(prop)
    return _property_out(db, prop)


@router.put("/{property_id}", response_model=PropertyOut)
def update_property(
    property_id: str,
    payload: PropertyCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    for key, value in payload.model_dump().items():
        setattr(prop, key, value)
    db.commit()
    db.refresh(prop)
    return _property_out(db, prop)


@router.post("/{property_id}/block", response_model=PropertyOut)
def block_property(
    property_id: str,
    payload: PropertyBlockRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admin-only: prevent bookings for a property during a date range."""
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    if payload.end_date < payload.start_date:
        raise HTTPException(status_code=400, detail="End date must be on or after start date")

    conflict = (
        db.query(PropertyAvailability)
        .filter(
            PropertyAvailability.property_id == property_id,
            PropertyAvailability.status.in_(["booked", "blocked"]),
            PropertyAvailability.start_date <= payload.end_date,
            PropertyAvailability.end_date >= payload.start_date,
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
            property_id=property_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            status="blocked",
        )
    )
    db.commit()
    db.refresh(prop)
    return _property_out(db, prop)


@router.delete("/{property_id}/block", response_model=PropertyOut)
def clear_property_blocks(
    property_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admin-only: clear current and future owner-created date blocks."""
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    db.query(PropertyAvailability).filter(
        PropertyAvailability.property_id == property_id,
        PropertyAvailability.status == "blocked",
    ).delete(synchronize_session=False)
    db.commit()
    db.refresh(prop)
    return _property_out(db, prop)


@router.post("/{property_id}/offline", response_model=PropertyOut)
def take_property_offline(
    property_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admin-only: hide a property from public listings."""
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    prop.status = "inactive"
    db.commit()
    db.refresh(prop)
    return _property_out(db, prop)


@router.delete("/{property_id}")
def delete_property(
    property_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Soft-delete: hides property from public listings. Bookings are kept."""
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    if prop.status == "inactive":
        return {"ok": True, "message": "Property already removed"}
    prop.status = "inactive"
    db.commit()
    return {"ok": True, "message": "Property removed from listings"}


@router.post("/{property_id}/restore", response_model=PropertyOut)
def restore_property(
    property_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Re-publish a previously removed property."""
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    prop.status = "active"
    db.commit()
    db.refresh(prop)
    return _property_out(db, prop)
