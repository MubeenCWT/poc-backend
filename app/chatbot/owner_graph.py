"""Admin WhatsApp portfolio assistant (menu-driven).

Professional UX requirements:
- When admin says `hi`/`hello`/`menu`, show a menu (interactive buttons).
- Menu choices trigger deterministic flows:
  - Vacant apartments -> list vacant units (as of today)
  - Release date -> ask property -> show next free date
  - Block dates -> ask property -> ask dates -> create blocked availability rows
  - Take offline -> ask property -> ask dates -> block dates + mark property inactive
  - Bring online -> ask property -> mark property active again
"""

import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Booking, Property, PropertyAvailability, User


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def find_admin_by_phone(db: Session, phone: str) -> Optional[User]:
    """Treat admin WhatsApp number as the only 'owner/admin' for portfolio actions."""
    admin_digits = _digits(settings.ADMIN_WHATSAPP_NUMBER)
    if not admin_digits or _digits(phone) != admin_digits:
        return None
    return db.query(User).filter(User.role == "admin", User.is_active == True).first()  # noqa: E712


def portfolio_properties(db: Session) -> list[Property]:
    return db.query(Property).order_by(Property.title).all()


def match_portfolio_property(db: Session, query: str) -> Optional[Property]:
    q = (query or "").lower().strip()
    if not q or q == "unknown":
        return None
    for prop in portfolio_properties(db):
        title = (prop.title or "").lower()
        area = (prop.area or "").lower()
        if q in title or title in q or (area and (q in area or area in q)):
            return prop
    return None


def _is_occupied_on(db: Session, prop: Property, day: datetime.date) -> bool:
    if prop.status != "active":
        return True
    row = (
        db.query(PropertyAvailability)
        .filter(
            PropertyAvailability.property_id == prop.id,
            PropertyAvailability.status.in_(["booked", "blocked"]),
            PropertyAvailability.start_date <= day,
            PropertyAvailability.end_date >= day,
        )
        .first()
    )
    return row is not None


def portfolio_summary(db: Session, as_of: Optional[datetime.date] = None) -> dict:
    day = as_of or datetime.date.today()
    props = portfolio_properties(db)
    vacant = []
    occupied = []
    offline = []
    for prop in props:
        if prop.status != "active":
            offline.append(prop)
        elif _is_occupied_on(db, prop, day):
            occupied.append(prop)
        else:
            vacant.append(prop)
    return {
        "as_of": day,
        "total": len(props),
        "vacant": vacant,
        "occupied": occupied,
        "offline": offline,
    }


def next_release_info(
    db: Session, prop: Property, from_date: Optional[datetime.date] = None
) -> dict:
    day = from_date or datetime.date.today()
    future = (
        db.query(PropertyAvailability)
        .filter(
            PropertyAvailability.property_id == prop.id,
            PropertyAvailability.status.in_(["booked", "blocked"]),
            PropertyAvailability.end_date >= day,
        )
        .order_by(PropertyAvailability.end_date.desc())
        .all()
    )

    if not future:
        if prop.status != "active":
            return {"status": "offline", "available_from": None, "message": "Property is offline."}
        if _is_occupied_on(db, prop, day):
            return {"status": "occupied", "available_from": None, "message": "Occupied but no end date."}
        return {"status": "available", "available_from": day, "message": "Available now."}

    latest_end = max(r.end_date for r in future)
    currently_busy = any(r.start_date <= day <= r.end_date for r in future)
    if not currently_busy and prop.status == "active":
        return {"status": "available", "available_from": day, "message": "Available now."}

    release = latest_end + datetime.timedelta(days=1)
    booking = (
        db.query(Booking)
        .filter(
            Booking.property_id == prop.id,
            Booking.status.in_(["pending", "confirmed"]),
            Booking.end_date == latest_end,
        )
        .order_by(Booking.end_date.desc())
        .first()
    )

    return {
        "status": "occupied",
        "available_from": release,
        "guest": booking.guest_name if booking else None,
        "booking_end": latest_end,
    }


def _parse_date(value: str) -> Optional[datetime.date]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _fmt_date(d: Optional[datetime.date]) -> str:
    if not d:
        return "—"
    return d.strftime("%d %b %Y")


def block_property_dates(
    db: Session,
    prop: Property,
    start_date: datetime.date,
    end_date: datetime.date,
) -> PropertyAvailability:
    if end_date < start_date:
        raise ValueError("End date must be on or after start date.")

    overlap = (
        db.query(PropertyAvailability)
        .filter(
            PropertyAvailability.property_id == prop.id,
            PropertyAvailability.status.in_(["booked", "blocked"]),
            PropertyAvailability.start_date <= end_date,
            PropertyAvailability.end_date >= start_date,
        )
        .first()
    )
    if overlap:
        raise ValueError(
            f"Dates overlap an existing {overlap.status} period "
            f"({overlap.start_date} to {overlap.end_date})."
        )

    row = PropertyAvailability(
        property_id=prop.id,
        start_date=start_date,
        end_date=end_date,
        status="blocked",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def take_property_offline(
    db: Session,
    prop: Property,
    start_date: datetime.date,
    end_date: datetime.date,
) -> None:
    block_property_dates(db, prop, start_date, end_date)
    prop.status = "inactive"
    db.commit()
    db.refresh(prop)


def bring_property_online(db: Session, prop: Property) -> None:
    prop.status = "active"
    db.commit()
    db.refresh(prop)


MENU1: list[dict[str, str]] = [
    {"id": "pf_menu1_vacant", "title": "Vacant apartments"},
    {"id": "pf_menu1_release", "title": "Release date"},
    {"id": "pf_menu1_more", "title": "More options"},
]

MENU2: list[dict[str, str]] = [
    {"id": "pf_menu2_block", "title": "Block dates"},
    {"id": "pf_menu2_offline", "title": "Take offline"},
    {"id": "pf_menu2_online", "title": "Bring online"},
]


def _is_greeting(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("hi", "hello", "hey", "menu", "start")


def _property_list_text(props: list[Property]) -> str:
    if not props:
        return "(none)"
    return ", ".join(f"{p.title} ({p.area})" for p in props if p.title)


def _property_pick_buttons(props: list[Property]) -> list[dict[str, str]]:
    # WhatsApp reply buttons: max 3
    return [{"id": f"pf_pick_{p.id}", "title": (p.title or p.area or "Property")} for p in props[:3]]


def _vacant_reply(db: Session) -> str:
    summary = portfolio_summary(db)
    vacant = summary.get("vacant") or []
    if not vacant:
        return "*Vacant apartments:* none right now."
    lines = ["*Vacant apartments:*"]
    for p in vacant:
        lines.append(f"• {p.title} ({p.area})")
    return "\n".join(lines)


def _reply_release(db: Session, prop: Property) -> str:
    info = next_release_info(db, prop)
    if info["status"] == "available":
        return f"*{prop.title}* is available now — no active guest booking or block."
    if info["status"] == "offline":
        return f"*{prop.title}* is currently offline from guest bookings."

    release = info.get("available_from")
    guest = info.get("guest")
    end = info.get("booking_end")
    parts = [f"*{prop.title}*"]
    if guest:
        parts.append(f"Guest: {guest}")
    if end:
        parts.append(f"Current stay ends: {_fmt_date(end)}")
    if release:
        parts.append(f"Available for new bookings from: {_fmt_date(release)}")
    return "\n".join(parts)


async def handle_admin_portfolio_message(
    db: Session,
    text: str,
    state: dict,
) -> tuple[str, dict, list[dict[str, str]]]:
    """Return (reply_text, updated_state, reply_buttons)."""
    props = portfolio_properties(db)
    prop_names = _property_list_text(props)

    step = state.get("portfolio_step")

    # Step 1: pick property for release/block/offline/online
    if step == "pick_property":
        prop = match_portfolio_property(db, text)
        if not prop:
            return (
                f"I couldn't match that property.\n"
                f"Reply with the name/area (e.g. Marina), or tap a suggested unit below.\n"
                f"Your units: {prop_names}",
                state,
                _property_pick_buttons(props),
            )

        pending = state.get("portfolio_pending_intent")
        state.pop("portfolio_step", None)
        state.pop("portfolio_property_id", None)
        state.pop("portfolio_pending_intent", None)

        if pending == "release_date":
            return _reply_release(db, prop), {}, MENU1

        if pending == "bring_online":
            bring_property_online(db, prop)
            return f"*{prop.title}* is active again and visible for bookings.", {}, MENU1

        if pending in ("block_dates", "take_offline"):
            state["portfolio_property_id"] = prop.id
            state["portfolio_pending_intent"] = pending
            state["portfolio_step"] = "ask_start_date"
            action = "block bookings" if pending == "block_dates" else "take it offline"
            return (
                f"Got it — *{prop.title}*.\n"
                f"From which date should I {action}? (YYYY-MM-DD)",
                state,
                [],
            )

        return "Please choose an option again (hi/menu).", {}, MENU1

    # Step 2: ask start date
    if step == "ask_start_date":
        start = _parse_date(text)
        if not start:
            return "Please send a start date as YYYY-MM-DD (e.g. 2026-08-01).", state, []
        state["portfolio_start_date"] = start.isoformat()
        state["portfolio_step"] = "ask_end_date"
        return "And the end date? (YYYY-MM-DD)", state, []

    # Step 3: ask end date and execute
    if step == "ask_end_date":
        end = _parse_date(text)
        start = _parse_date(state.get("portfolio_start_date", ""))
        pending = state.get("portfolio_pending_intent", "block_dates")
        prop_id = state.get("portfolio_property_id")
        prop = db.get(Property, prop_id) if prop_id else None

        if not end or not start or not prop:
            return "Something went wrong — please start again with `hi` or `menu`.", {}, MENU1
        if end < start:
            return "End date must be on or after start date. Send the end date again.", state, []

        try:
            if pending == "take_offline":
                take_property_offline(db, prop, start, end)
                return (
                    f"Done. *{prop.title}* is offline until {_fmt_date(end)} ({start} to {end}).",
                    {},
                    MENU1,
                )
            block_property_dates(db, prop, start, end)
            return (
                f"Done. *{prop.title}* is blocked from {_fmt_date(start)} to {_fmt_date(end)}.",
                {},
                MENU1,
            )
        except ValueError as exc:
            return f"Couldn't do that: {exc}", {}, MENU1

    # Fresh message
    t = (text or "").strip().lower()

    # Always show menu on greeting/menu words.
    if _is_greeting(text):
        return "Menu (UAE Stays admin):\n\nChoose an option:", {}, MENU1

    if "more" in t:
        return "More options:", {}, MENU2

    if "vacant" in t or "not on rent" in t:
        return _vacant_reply(db), {}, MENU1

    if "release" in t or "checkout" in t or "guest leave" in t:
        state["portfolio_step"] = "pick_property"
        state["portfolio_pending_intent"] = "release_date"
        return (
            "Which property do you want the release date for?\n"
            "Reply with the name or area (e.g. Marina).",
            state,
            _property_pick_buttons(props),
        )

    if "block" in t or "blackout" in t:
        state["portfolio_step"] = "pick_property"
        state["portfolio_pending_intent"] = "block_dates"
        return (
            "Which property should I block?\n"
            "Reply with the name or area (e.g. JBR).",
            state,
            _property_pick_buttons(props),
        )

    if "offline" in t or "take offline" in t or "unlist" in t:
        state["portfolio_step"] = "pick_property"
        state["portfolio_pending_intent"] = "take_offline"
        return (
            "Which property should I take offline?\n"
            "Reply with the name or area (e.g. Downtown).",
            state,
            _property_pick_buttons(props),
        )

    if "bring online" in t or ("online" in t and "bring" in t):
        state["portfolio_step"] = "pick_property"
        state["portfolio_pending_intent"] = "bring_online"
        return (
            "Which property should go back online?\n"
            "Reply with the name or area.",
            state,
            _property_pick_buttons(props),
        )

    # Unknown: show menu instead of dumping everything.
    return (
        "I didn't understand that.\n\nSend `hi` to see the admin menu.",
        {},
        MENU1,
    )


# Older deployments/imports
handle_owner_message = handle_admin_portfolio_message

