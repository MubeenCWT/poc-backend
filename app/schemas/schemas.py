from datetime import date, datetime
from typing import Optional, List
from pydantic import BaseModel


# ---------- Auth ----------
class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------- Property ----------
class PropertyCreate(BaseModel):
    title: str
    description: Optional[str] = None
    property_type: Optional[str] = None
    emirate: str
    area: Optional[str] = None
    address: Optional[str] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    max_guests: Optional[int] = None
    amenities: List[str] = []
    images: List[str] = []
    price_daily: Optional[float] = None
    price_monthly: Optional[float] = None
    price_yearly: Optional[float] = None


class PropertyOut(PropertyCreate):
    id: str
    owner_id: Optional[str] = None
    status: str
    created_at: datetime
    block_active: bool = False
    block_start: Optional[date] = None
    block_end: Optional[date] = None
    listing_label: str = "live"  # live | blocked | blocked_soon | removed

    class Config:
        from_attributes = True


# ---------- Booking ----------
class BookingCreate(BaseModel):
    property_id: str
    guest_name: str
    guest_phone: str
    booking_type: str  # daily, monthly, yearly
    start_date: date
    end_date: date
    discount_requested: bool = False
    discount_amount: Optional[float] = 0
    source: str = "chatbot"


class BookingOut(BaseModel):
    id: str
    property_id: str
    guest_name: Optional[str]
    guest_phone: Optional[str]
    booking_type: str
    start_date: date
    end_date: date
    base_price: float
    discount_requested: bool = False
    discount_amount: Optional[float] = 0
    discount_status: str
    final_price: Optional[float]
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Vendor ----------
class VendorCreate(BaseModel):
    name: str
    service_type: str
    phone: str
    email: Optional[str] = None
    coverage_areas: List[str] = []


class VendorOut(VendorCreate):
    id: str
    is_active: bool

    class Config:
        from_attributes = True


class VendorAssign(BaseModel):
    vendor_id: str


# ---------- Maintenance ----------
class MaintenanceCreate(BaseModel):
    property_id: str
    requested_by: str
    issue_type: str
    description: str


# ---------- Discount ----------
class DiscountDecision(BaseModel):
    approve: bool = True
    counter_amount: Optional[float] = None  # if set, sends a counter-offer instead


class CounterResponse(BaseModel):
    accept: bool
