from typing import List, Optional
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Property, User
from app.schemas.schemas import PropertyCreate, PropertyOut
from app.services.deps import get_current_admin

router = APIRouter(prefix="/properties", tags=["properties"])


@router.get("/", response_model=List[PropertyOut])
def list_properties(
    emirate: Optional[str] = None,
    area: Optional[str] = None,
    property_type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Public endpoint - powers the website listing page and the chatbot's search."""
    query = db.query(Property).filter(Property.status == "active")
    if emirate:
        query = query.filter(Property.emirate == emirate)
    if area:
        query = query.filter(Property.area == area)
    if property_type:
        query = query.filter(Property.property_type == property_type)
    return query.all()


@router.get("/{property_id}", response_model=PropertyOut)
def get_property(property_id: str, db: Session = Depends(get_db)):
    return db.query(Property).filter(Property.id == property_id).first()


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
    return prop


@router.put("/{property_id}", response_model=PropertyOut)
def update_property(
    property_id: str,
    payload: PropertyCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    prop = db.query(Property).filter(Property.id == property_id).first()
    for key, value in payload.model_dump().items():
        setattr(prop, key, value)
    db.commit()
    db.refresh(prop)
    return prop


@router.delete("/{property_id}")
def delete_property(
    property_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    prop = db.query(Property).filter(Property.id == property_id).first()
    prop.status = "inactive"
    db.commit()
    return {"ok": True}
