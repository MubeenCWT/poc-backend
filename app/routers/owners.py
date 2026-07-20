from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Property, User
from app.services.deps import get_current_admin

router = APIRouter(prefix="/owners", tags=["owners"])


class OwnerCreate(BaseModel):
    full_name: str
    phone: str
    email: Optional[str] = None


class OwnerOut(BaseModel):
    id: str
    full_name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    is_active: bool

    class Config:
        from_attributes = True


class AssignOwnerRequest(BaseModel):
    owner_id: Optional[str] = None


@router.get("/", response_model=List[OwnerOut])
def list_owners(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    return (
        db.query(User)
        .filter(User.role == "owner")
        .order_by(User.full_name)
        .all()
    )


@router.post("/", response_model=OwnerOut)
def create_owner(
    payload: OwnerCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    phone = payload.phone.strip()
    if not phone.startswith("+"):
        phone = f"+{phone.lstrip('+')}"
    existing = db.query(User).filter(User.phone == phone).first()
    if existing:
        raise HTTPException(400, "A user with this phone already exists")
    owner = User(
        role="owner",
        full_name=payload.full_name.strip(),
        phone=phone,
        email=payload.email,
        is_active=True,
    )
    db.add(owner)
    db.commit()
    db.refresh(owner)
    return owner


@router.patch("/properties/{property_id}/assign", response_model=dict)
def assign_property_owner(
    property_id: str,
    payload: AssignOwnerRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(404, "Property not found")
    if payload.owner_id:
        owner = db.query(User).filter(User.id == payload.owner_id, User.role == "owner").first()
        if not owner:
            raise HTTPException(404, "Owner not found")
        prop.owner_id = owner.id
    else:
        prop.owner_id = admin.id
    db.commit()
    return {"ok": True, "property_id": property_id, "owner_id": prop.owner_id}
