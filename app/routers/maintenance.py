from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import MaintenanceRequest, Property, Vendor, AdminNotification, User
from app.schemas.schemas import MaintenanceCreate, VendorAssign
from app.services.deps import get_current_admin
from app.services.notify import send_admin_alert

router = APIRouter(prefix="/maintenance", tags=["maintenance"])


async def _push_alert(message: str):
    await send_admin_alert(message)


def _match_vendors(db: Session, issue_type: str, area: str | None) -> list[Vendor]:
    """All active vendors of this service type. If some cover the property's
    area, prefer those; otherwise return every vendor of the type."""
    vendors = (
        db.query(Vendor)
        .filter(Vendor.service_type == issue_type, Vendor.is_active == True)  # noqa: E712
        .all()
    )
    if area:
        area_matches = [v for v in vendors if v.coverage_areas and area in v.coverage_areas]
        if area_matches:
            return area_matches
    return vendors


@router.post("/")
async def create_maintenance_request(
    payload: MaintenanceCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    prop = db.query(Property).filter(Property.id == payload.property_id).first()

    candidates = _match_vendors(db, payload.issue_type, prop.area if prop else None)
    # Only auto-assign when there's a single obvious choice; otherwise the admin picks.
    auto = candidates[0] if len(candidates) == 1 else None

    request = MaintenanceRequest(
        property_id=payload.property_id,
        requested_by=payload.requested_by,
        issue_type=payload.issue_type,
        description=payload.description,
        vendor_id=auto.id if auto else None,
        status="assigned" if auto else "open",
        admin_notified=True,
    )
    db.add(request)
    db.flush()

    if auto:
        vendor_line = f"Assigned vendor: {auto.name} ({auto.phone})"
    elif candidates:
        listing = "\n".join(f"  - {v.name} ({v.phone})" for v in candidates)
        vendor_line = (
            f"{len(candidates)} vendors available — choose one in the admin portal:\n{listing}"
        )
    else:
        vendor_line = "No matching vendor found — please assign manually."

    message = (
        f"Maintenance request:\n"
        f"Property: {prop.title if prop else payload.property_id}\n"
        f"Reported by: {payload.requested_by}\n"
        f"Issue: {payload.issue_type} - {payload.description}\n"
        f"{vendor_line}"
    )

    db.add(AdminNotification(type="maintenance", reference_id=request.id, message=message))
    db.commit()

    background_tasks.add_task(_push_alert, message)

    return {
        "ok": True,
        "request_id": request.id,
        "vendor_assigned": auto.name if auto else None,
        "candidate_count": len(candidates),
    }


@router.get("/")
def list_maintenance_requests(db: Session = Depends(get_db)):
    return db.query(MaintenanceRequest).order_by(MaintenanceRequest.created_at.desc()).all()


@router.post("/{request_id}/assign")
async def assign_vendor(
    request_id: str,
    payload: VendorAssign,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admin assigns one of the candidate vendors to a maintenance request."""
    request = db.query(MaintenanceRequest).filter(MaintenanceRequest.id == request_id).first()
    if not request:
        raise HTTPException(404, "Maintenance request not found")

    vendor = db.query(Vendor).filter(Vendor.id == payload.vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    request.vendor_id = vendor.id
    request.status = "assigned"
    db.commit()
    db.refresh(request)
    return request
