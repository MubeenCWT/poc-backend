"""Dedicated admin property-management API.

Kept separate from public property routes so block/offline/remove operations
cannot be hidden by an older or conflicting properties router.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import User
from app.schemas.schemas import PropertyBlockRequest, PropertyOut
from app.services.deps import get_current_admin
from app.services.property_management import (
    block_dates,
    clear_blocks,
    get_property_or_404,
    set_listing_status,
)
from app.services.property_status import serialize_property

router = APIRouter(prefix="/property-management", tags=["property-management"])


def _out(db: Session, prop) -> PropertyOut:
    return PropertyOut(**serialize_property(db, prop))


@router.get("/health")
def property_management_health(
    admin: User = Depends(get_current_admin),
):
    return {"ok": True, "service": "property-management", "version": 1}


@router.post("/{property_id}/block", response_model=PropertyOut)
def block_property_dates(
    property_id: str,
    payload: PropertyBlockRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    prop = get_property_or_404(db, property_id)
    return _out(db, block_dates(db, prop, payload.start_date, payload.end_date))


@router.delete("/{property_id}/block", response_model=PropertyOut)
def clear_property_date_blocks(
    property_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    prop = get_property_or_404(db, property_id)
    return _out(db, clear_blocks(db, prop))


@router.post("/{property_id}/offline", response_model=PropertyOut)
def take_property_offline(
    property_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    prop = get_property_or_404(db, property_id)
    return _out(db, set_listing_status(db, prop, "offline"))


@router.post("/{property_id}/restore", response_model=PropertyOut)
def restore_property_listing(
    property_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    prop = get_property_or_404(db, property_id)
    return _out(db, set_listing_status(db, prop, "active"))


@router.delete("/{property_id}", response_model=PropertyOut)
def remove_property_listing(
    property_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    prop = get_property_or_404(db, property_id)
    return _out(db, set_listing_status(db, prop, "inactive"))

