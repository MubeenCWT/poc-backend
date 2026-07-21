from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from sqlalchemy import text

from app.database import Base, engine
from app.models import models
from app.routers import (
    auth,
    bookings,
    chat,
    maintenance,
    notifications,
    properties,
    property_management,
    vendors,
    whatsapp_webhook,
)


def _ensure_columns():
    """Add columns that create_all won't alter on existing SQLite tables."""
    with engine.begin() as conn:
        try:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(discount_requests)")).fetchall()}
            if "counter_amount" not in cols:
                conn.execute(text("ALTER TABLE discount_requests ADD COLUMN counter_amount NUMERIC(10, 2)"))
        except Exception:
            pass  # non-sqlite or table not ready yet


@asynccontextmanager
async def lifespan(app: FastAPI):
    from seed_data import run_seed
    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    run_seed()
    yield

app = FastAPI(title="UAE Real Estate Booking Platform", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(properties.router, prefix="/api")
app.include_router(property_management.router, prefix="/api")
app.include_router(bookings.router, prefix="/api")
app.include_router(vendors.router, prefix="/api")
app.include_router(maintenance.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(notifications.router, prefix="/api")
app.include_router(whatsapp_webhook.router)  # no /api prefix — Meta calls /webhook/whatsapp

@app.get("/api")
def root():
    return {"status": "ok", "service": "uae-realestate-api"}
