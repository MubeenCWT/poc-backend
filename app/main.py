from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.database import Base, engine
from app.models import models
from app.routers import auth, properties, bookings, vendors, maintenance, chat, notifications, whatsapp_webhook

@asynccontextmanager
async def lifespan(app: FastAPI):
    from seed_data import run_seed
    Base.metadata.create_all(bind=engine)
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
app.include_router(bookings.router, prefix="/api")
app.include_router(vendors.router, prefix="/api")
app.include_router(maintenance.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(notifications.router, prefix="/api")
app.include_router(whatsapp_webhook.router)  # no /api prefix — Meta calls /webhook/whatsapp

@app.get("/api")
def root():
    return {"status": "ok", "service": "uae-realestate-api"}
