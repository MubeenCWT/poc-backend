import uuid
from sqlalchemy import (
    Column, String, Text, Boolean, Numeric, Integer, Date, DateTime,
    ForeignKey, CheckConstraint, JSON
)
from sqlalchemy.sql import func
from app.database import Base


def gen_uuid():
    return str(uuid.uuid4())


# UUIDs are stored as plain 36-char strings (SQLite has no native UUID type).
# JSON columns use SQLAlchemy's generic JSON type, which SQLite stores as TEXT
# under the hood and (de)serializes automatically -- no extra code needed.

class User(Base):
    __tablename__ = "users"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    role = Column(String(20), nullable=False)  # admin, owner, tenant, vendor
    full_name = Column(String(150), nullable=False)
    email = Column(String(150), unique=True, nullable=True)
    phone = Column(String(20), unique=True, nullable=True)
    password_hash = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class Property(Base):
    __tablename__ = "properties"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    owner_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    property_type = Column(String(50))
    emirate = Column(String(50), nullable=False)
    area = Column(String(100))
    address = Column(Text)
    bedrooms = Column(Integer)
    bathrooms = Column(Integer)
    max_guests = Column(Integer)
    amenities = Column(JSON, default=list)
    images = Column(JSON, default=list)
    price_daily = Column(Numeric(10, 2))
    price_monthly = Column(Numeric(10, 2))
    price_yearly = Column(Numeric(12, 2))
    status = Column(String(20), default="active")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now())


class PropertyAvailability(Base):
    __tablename__ = "property_availability"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    property_id = Column(String(36), ForeignKey("properties.id", ondelete="CASCADE"), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False)  # booked, blocked, maintenance
    booking_id = Column(String(36), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (CheckConstraint("end_date >= start_date"),)


class Booking(Base):
    __tablename__ = "bookings"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    property_id = Column(String(36), ForeignKey("properties.id"), nullable=False)
    tenant_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    guest_name = Column(String(150))
    guest_phone = Column(String(20))
    booking_type = Column(String(10), nullable=False)  # daily, monthly, yearly
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    base_price = Column(Numeric(10, 2), nullable=False)
    discount_requested = Column(Boolean, default=False)
    discount_amount = Column(Numeric(10, 2), default=0)
    discount_status = Column(String(20), default="none")  # none, pending, approved, rejected, countered
    final_price = Column(Numeric(10, 2))
    status = Column(String(20), default="pending")  # pending, confirmed, cancelled, completed
    source = Column(String(20), default="chatbot")
    created_at = Column(DateTime, server_default=func.now())


class Vendor(Base):
    __tablename__ = "vendors"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    name = Column(String(150), nullable=False)
    service_type = Column(String(50), nullable=False)  # plumbing, electrical, AC, cleaning
    phone = Column(String(20), nullable=False)
    email = Column(String(150))
    coverage_areas = Column(JSON, default=list)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class MaintenanceRequest(Base):
    __tablename__ = "maintenance_requests"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    property_id = Column(String(36), ForeignKey("properties.id"), nullable=False)
    booking_id = Column(String(36), ForeignKey("bookings.id"), nullable=True)
    requested_by = Column(String(150))
    issue_type = Column(String(50))
    description = Column(Text)
    vendor_id = Column(String(36), ForeignKey("vendors.id"), nullable=True)
    status = Column(String(20), default="open")
    admin_notified = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())


class DiscountRequest(Base):
    __tablename__ = "discount_requests"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    booking_id = Column(String(36), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    requested_amount = Column(Numeric(10, 2))
    counter_amount = Column(Numeric(10, 2), nullable=True)
    reason = Column(Text)
    status = Column(String(20), default="pending")  # pending, approved, rejected, countered
    decided_by = Column(String(36), ForeignKey("users.id"), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class ChatbotSession(Base):
    __tablename__ = "chatbot_sessions"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    phone = Column(String(20), nullable=False)
    state = Column(JSON, default=dict)
    last_intent = Column(String(50))
    updated_at = Column(DateTime, server_default=func.now())


class ChatbotMessage(Base):
    __tablename__ = "chatbot_messages"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    session_id = Column(String(36), ForeignKey("chatbot_sessions.id", ondelete="CASCADE"))
    direction = Column(String(10))  # inbound, outbound
    message_text = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class AdminNotification(Base):
    __tablename__ = "admin_notifications"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    type = Column(String(30))  # maintenance, discount_request, new_booking
    reference_id = Column(String(36), nullable=True)
    message = Column(Text)
    sent_at = Column(DateTime, server_default=func.now())
    delivered = Column(Boolean, default=False)
