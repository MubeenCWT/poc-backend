from typing import List
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Vendor, User
from app.schemas.schemas import VendorCreate, VendorOut
from app.services.deps import get_current_admin

router = APIRouter(prefix="/vendors", tags=["vendors"])


@router.get("/", response_model=List[VendorOut])
def list_vendors(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    return db.query(Vendor).filter(Vendor.is_active == True).all()  # noqa: E712


@router.post("/", response_model=VendorOut)
def create_vendor(payload: VendorCreate, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    vendor = Vendor(**payload.model_dump())
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor

@router.put("/{vendor_id}", response_model=VendorOut)
def update_vendor(vendor_id: str, payload: VendorCreate, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    for key, value in payload.model_dump().items():
        setattr(vendor, key, value)
    db.commit()
    db.refresh(vendor)
    return vendor


@router.delete("/{vendor_id}")
def deactivate_vendor(vendor_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    vendor.is_active = False
    db.commit()
    return {"ok": True}
