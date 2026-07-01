from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import MaintenanceRequest, Property, Vendor, AdminNotification
from app.schemas.schemas import MaintenanceCreate
#from app.services.whatsapp import notify_admin

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.post("/")
def create_maintenance_request(payload: MaintenanceCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Called by the chatbot when a tenant reports an issue.
    Finds a matching vendor by service type + property area, then alerts admin with vendor info.
    """
    prop = db.query(Property).filter(Property.id == payload.property_id).first()

    vendor = (
        db.query(Vendor)
        .filter(Vendor.service_type == payload.issue_type, Vendor.is_active == True)  # noqa: E712
        .first()
    )
    # Prefer a vendor covering the property's area if one exists
    if vendor and prop and prop.area:
        area_match = (
            db.query(Vendor)
            .filter(Vendor.service_type == payload.issue_type, Vendor.is_active == True)  # noqa: E712
            .filter(Vendor.coverage_areas.contains([prop.area]))
            .first()
        )
        if area_match:
            vendor = area_match

    request = MaintenanceRequest(
        property_id=payload.property_id,
        requested_by=payload.requested_by,
        issue_type=payload.issue_type,
        description=payload.description,
        vendor_id=vendor.id if vendor else None,
        status="assigned" if vendor else "open",
        admin_notified=True,
    )
    db.add(request)
    db.flush()

    vendor_line = (
        f"Suggested vendor: {vendor.name} ({vendor.phone})" if vendor
        else "No matching vendor found — please assign manually."
    )
    message = (
        f"Maintenance request:\n"
        f"Property: {prop.title if prop else payload.property_id}\n"
        f"Reported by: {payload.requested_by}\n"
        f"Issue: {payload.issue_type} - {payload.description}\n"
        f"{vendor_line}"
    )
    db.add(AdminNotification(type="maintenance", reference_id=request.id, message=message))
    db.commit()

    db.commit()

    return {"ok": True, "request_id": request.id, "vendor_assigned": vendor.name if vendor else None}

@router.get("/")
def list_maintenance_requests(db: Session = Depends(get_db)):
    """Admin-only: view all maintenance requests in the portal."""
    return db.query(MaintenanceRequest).order_by(MaintenanceRequest.created_at.desc()).all()
