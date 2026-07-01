from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import AdminNotification, User
from app.services.deps import get_current_admin
from typing import List

router = APIRouter(prefix="/notifications", tags=["notifications"])

@router.get("/")
def list_notifications(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    return db.query(AdminNotification).order_by(AdminNotification.sent_at.desc()).all()

@router.get("/count")
def get_unread_count(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    count = db.query(AdminNotification).filter(AdminNotification.delivered == False).count()
    return {"count": count}

@router.patch("/{notif_id}/read")
def mark_as_read(notif_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    notif = db.query(AdminNotification).filter(AdminNotification.id == notif_id).first()
    if notif:
        notif.delivered = True
        db.commit()
    return {"ok": True}

@router.patch("/read-all")
def mark_all_as_read(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
    db.query(AdminNotification).filter(AdminNotification.delivered == False).update({"delivered": True})
    db.commit()
    return {"ok": True}
