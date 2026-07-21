"""Admin WhatsApp portfolio assistant — clear menu-driven flows."""

import calendar
import datetime
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Booking, Property, PropertyAvailability, User


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def find_admin_by_phone(db: Session, phone: str) -> Optional[User]:
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
    if q.startswith("pf_pick_"):
        prop_id = q.replace("pf_pick_", "", 1)
        return db.get(Property, prop_id)
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
    vacant, occupied, offline = [], [], []
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
            return {"status": "offline", "available_from": None}
        if _is_occupied_on(db, prop, day):
            return {"status": "occupied", "available_from": None}
        return {"status": "available", "available_from": day}
    latest_end = max(r.end_date for r in future)
    currently_busy = any(r.start_date <= day <= r.end_date for r in future)
    if not currently_busy and prop.status == "active":
        return {"status": "available", "available_from": day}
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


def _add_months(d: datetime.date, months: int) -> datetime.date:
    idx = d.month - 1 + months
    year = d.year + idx // 12
    month = idx % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def _parse_one_date(raw: str, current_year: Optional[int] = None) -> Optional[datetime.date]:
    raw = (raw or "").strip()
    if not raw:
        return None
    year = current_year or datetime.date.today().year
    lower = raw.lower()
    today = datetime.date.today()

    if "next month" in lower:
        return _add_months(today.replace(day=1), 1)
    if "this month" in lower:
        return today.replace(day=1)
    if lower in ("today", "now"):
        return today
    if lower == "tomorrow":
        return today + datetime.timedelta(days=1)

    iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", raw)
    if iso:
        try:
            return datetime.date.fromisoformat(iso.group(1))
        except ValueError:
            pass

    for fmt in (
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
        "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
        "%b %d, %Y", "%B %d, %Y",
        "%d %b", "%d %B", "%b %d", "%B %d",
        "%d/%m", "%d-%m",
    ):
        try:
            parsed = datetime.datetime.strptime(raw.strip(), fmt).date()
            if parsed.year == 1900 or fmt in ("%d %b", "%d %B", "%b %d", "%B %d", "%d/%m", "%d-%m"):
                parsed = parsed.replace(year=year)
            return parsed
        except ValueError:
            continue

    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)(?:\s+(\d{4}))?\b", lower)
    if m:
        day, mon, yr = m.group(1), m.group(2), m.group(3)
        try:
            yr = int(yr) if yr else year
            parsed = datetime.datetime.strptime(f"{day} {mon} {yr}", "%d %B %Y").date()
            return parsed
        except ValueError:
            try:
                parsed = datetime.datetime.strptime(f"{day} {mon} {yr}", "%d %b %Y").date()
                return parsed
            except ValueError:
                pass
    return None


def _parse_date_range(text: str) -> tuple[Optional[datetime.date], Optional[datetime.date]]:
    """Parse flexible admin date ranges: '1 Aug to 15 Aug', '01/08/2026 - 15/08/2026'."""
    raw = (text or "").strip()
    if not raw:
        return None, None

    separators = r"\s+(?:to|until|through|till|-)\s+"
    parts = re.split(separators, raw, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        start = _parse_one_date(parts[0].strip())
        end = _parse_one_date(parts[1].strip())
        if start and end:
            return start, end

    dates = []
    for token in re.findall(
        r"\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?|"
        r"\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?[a-z]+(?:\s+\d{4})?",
        raw,
        flags=re.I,
    ):
        d = _parse_one_date(token)
        if d:
            dates.append(d)
    if len(dates) >= 2:
        return dates[0], dates[1]
    if len(dates) == 1:
        return dates[0], None
    return None, None


def _try_parse_block_command(
    db: Session, text: str
) -> tuple[Optional[Property], Optional[datetime.date], Optional[datetime.date]] | None:
    """One-shot: 'block Marina from 1 Aug to 15 Aug'."""
    lower = (text or "").lower()
    if "block" not in lower:
        return None
    start, end = _parse_date_range(text)
    if not start or not end:
        return None
    prop_part = re.sub(r"block\s+", "", lower, count=1, flags=re.I)
    prop_part = re.split(r"\s+from\s+|\s+between\s+", prop_part, maxsplit=1, flags=re.I)[0]
    prop_part = prop_part.strip(" .,")
    prop = match_portfolio_property(db, prop_part) if prop_part else None
    if not prop:
        return None
    return prop, start, end


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
            f"({_fmt_date(overlap.start_date)} to {_fmt_date(overlap.end_date)})."
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
    prop.status = "offline"
    db.commit()
    db.refresh(prop)


def bring_property_online(db: Session, prop: Property) -> None:
    prop.status = "active"
    db.commit()
    db.refresh(prop)


def remove_property_listing(db: Session, prop: Property) -> None:
    prop.status = "inactive"
    db.commit()
    db.refresh(prop)


# ---- Menus (WhatsApp max 3 buttons) ----
MENU_HOME = [
    {"id": "pf_act_vacant", "title": "Vacant units"},
    {"id": "pf_act_release", "title": "Release date"},
    {"id": "pf_act_manage", "title": "Manage property"},
]

MENU_MANAGE = [
    {"id": "pf_act_block", "title": "Block dates"},
    {"id": "pf_act_offline", "title": "Take offline"},
    {"id": "pf_act_remove", "title": "Remove listing"},
]

MENU_MANAGE2 = [
    {"id": "pf_act_online", "title": "Bring online"},
    {"id": "pf_act_home", "title": "Main menu"},
]


def _menu_home_text() -> str:
    return (
        "*UAE Stays — Admin Menu*\n\n"
        "• *Vacant units* — see what's free now\n"
        "• *Release date* — when guest checks out\n"
        "• *Manage property* — block, offline, remove\n\n"
        "Tap a button below:"
    )


def _menu_manage_text() -> str:
    return (
        "*Manage property*\n\n"
        "• *Block dates* — stop bookings (shows *Blocked* tag on site)\n"
        "• *Take offline* — hide from website entirely\n"
        "• *Remove listing* — delete from website\n"
        "• *Bring online* — list again (next menu)\n\n"
        "Tip: you can also say:\n"
        "_Block Marina from 1 Aug to 15 Aug_"
    )


def _property_pick_buttons(props: list[Property]) -> list[dict[str, str]]:
    buttons = []
    for p in props[:3]:
        title = (p.title or p.area or "Property")[:20]
        buttons.append({"id": f"pf_pick_{p.id}", "title": title})
    return buttons


def _vacant_reply(db: Session) -> str:
    summary = portfolio_summary(db)
    vacant = summary.get("vacant") or []
    if not vacant:
        return "*Vacant units:* none right now."
    lines = ["*Vacant units:*"]
    for p in vacant:
        lines.append(f"• {p.title} ({p.area})")
    return "\n".join(lines)


def _reply_release(db: Session, prop: Property) -> str:
    info = next_release_info(db, prop)
    if info["status"] == "available":
        return f"*{prop.title}* is available now."
    if info["status"] == "offline":
        return f"*{prop.title}* is offline from the website."
    parts = [f"*{prop.title}*"]
    if info.get("guest"):
        parts.append(f"Guest: {info['guest']}")
    if info.get("booking_end"):
        parts.append(f"Stay ends: {_fmt_date(info['booking_end'])}")
    if info.get("available_from"):
        parts.append(f"Free from: *{_fmt_date(info['available_from'])}*")
    return "\n".join(parts)


def _resolve_action(button_id: Optional[str], text: str) -> Optional[str]:
    bid = (button_id or "").strip()
    if bid.startswith("pf_act_"):
        return bid.replace("pf_act_", "", 1)
    if bid.startswith("pf_pick_"):
        return f"pick:{bid.replace('pf_pick_', '', 1)}"
    if bid == "pf_confirm_yes":
        return "confirm_delete_yes"
    if bid == "pf_confirm_no":
        return "confirm_delete_no"

    t = (text or "").strip().lower()
    mapping = {
        "vacant": "vacant",
        "vacant units": "vacant",
        "vacant apartments": "vacant",
        "release date": "release",
        "release": "release",
        "manage property": "manage",
        "more options": "manage",
        "block dates": "block",
        "block": "block",
        "take offline": "offline",
        "remove listing": "remove",
        "remove": "remove",
        "delete": "remove",
        "bring online": "online",
        "main menu": "home",
        "menu": "home",
        "yes, remove": "confirm_delete_yes",
        "yes remove": "confirm_delete_yes",
        "no, keep": "confirm_delete_no",
    }
    for key, action in mapping.items():
        if t == key or t.startswith(key):
            return action
    return None


async def handle_admin_portfolio_message(
    db: Session,
    text: str,
    state: dict,
    button_id: Optional[str] = None,
) -> tuple[str, dict, list[dict[str, str]]]:
    props = portfolio_properties(db)
    prop_names = ", ".join(f"{p.title}" for p in props[:8]) or "(none)"

    # One-shot block command
    block_cmd = _try_parse_block_command(db, text)
    if block_cmd:
        prop, start, end = block_cmd
        try:
            block_property_dates(db, prop, start, end)
            return (
                f"Done. *{prop.title}* blocked {_fmt_date(start)} → {_fmt_date(end)}.\n"
                f"It now shows a *Temporarily blocked* tag on the website.",
                {},
                MENU_HOME,
            )
        except ValueError as exc:
            return f"Couldn't block: {exc}", {}, MENU_HOME

    step = state.get("portfolio_step")
    action = _resolve_action(button_id, text)

    if step == "confirm_delete":
        prop_id = state.get("portfolio_property_id")
        prop = db.get(Property, prop_id) if prop_id else None
        if action in ("confirm_delete_yes",) or (text or "").strip().lower() in ("yes", "confirm", "remove"):
            if prop:
                remove_property_listing(db, prop)
                state = {}
                return (
                    f"Removed *{prop.title}* from the website.\n"
                    "Existing bookings are kept. Restore anytime from the admin portal or *Bring online*.",
                    state,
                    MENU_HOME,
                )
        state = {}
        return "Cancelled — listing kept.", state, MENU_HOME

    if step == "pick_property":
        prop = match_portfolio_property(db, text)
        if button_id and button_id.startswith("pf_pick_"):
            prop = db.get(Property, button_id.replace("pf_pick_", "", 1))
        if not prop:
            return (
                f"Which property?\nReply with name/area (e.g. Marina).\n\nUnits: {prop_names}",
                state,
                _property_pick_buttons(props),
            )
        pending = state.get("portfolio_pending_intent")
        state.pop("portfolio_step", None)
        state.pop("portfolio_pending_intent", None)

        if pending == "release_date":
            return _reply_release(db, prop), {}, MENU_HOME
        if pending == "bring_online":
            bring_property_online(db, prop)
            return f"*{prop.title}* is live on the website again.", {}, MENU_HOME
        if pending == "remove_listing":
            state["portfolio_property_id"] = prop.id
            state["portfolio_step"] = "confirm_delete"
            return (
                f"Remove *{prop.title}* from the website?\n"
                "Existing bookings stay. Reply *yes* to confirm or *no* to cancel.",
                state,
                [
                    {"id": "pf_confirm_yes", "title": "Yes, remove"},
                    {"id": "pf_confirm_no", "title": "No, keep"},
                ],
            )
        if pending in ("block_dates", "take_offline"):
            state["portfolio_property_id"] = prop.id
            state["portfolio_pending_intent"] = pending
            state["portfolio_step"] = "ask_dates"
            verb = "block bookings for" if pending == "block_dates" else "take offline"
            return (
                f"*{prop.title}* — send dates to {verb}.\n"
                "Examples: `1 Aug to 15 Aug` or `01/08/2026 to 15/08/2026`",
                state,
                [],
            )
        return _menu_home_text(), {}, MENU_HOME

    if step == "ask_dates":
        start, end = _parse_date_range(text)
        pending = state.get("portfolio_pending_intent", "block_dates")
        prop_id = state.get("portfolio_property_id")
        prop = db.get(Property, prop_id) if prop_id else None
        if not prop:
            return "Start again from the menu (send `hi`).", {}, MENU_HOME
        if not start:
            return (
                "Send the date range.\nExamples:\n• 1 Aug to 15 Aug\n• 2026-08-01 to 2026-08-15",
                state,
                [],
            )
        if not end:
            return f"Got start {_fmt_date(start)}. Now send the end date.", state, []
        if end < start:
            return "End date must be after start. Send the range again.", state, []
        try:
            if pending == "take_offline":
                take_property_offline(db, prop, start, end)
                return (
                    f"*{prop.title}* is offline until {_fmt_date(end)}.",
                    {},
                    MENU_HOME,
                )
            block_property_dates(db, prop, start, end)
            return (
                f"*{prop.title}* blocked {_fmt_date(start)} → {_fmt_date(end)}.\n"
                "Website shows *Temporarily blocked* tag.",
                {},
                MENU_HOME,
            )
        except ValueError as exc:
            return f"Couldn't do that: {exc}", {}, MENU_MANAGE

    # Menu actions
    if action == "home" or _is_greeting(text):
        return _menu_home_text(), {}, MENU_HOME
    if action == "manage":
        return _menu_manage_text(), {}, MENU_MANAGE
    if action == "online":
        state["portfolio_step"] = "pick_property"
        state["portfolio_pending_intent"] = "bring_online"
        return (
            "Which property should go back online?",
            state,
            _property_pick_buttons(props),
        )
    if action == "vacant":
        return _vacant_reply(db), {}, MENU_HOME
    if action == "release":
        state["portfolio_step"] = "pick_property"
        state["portfolio_pending_intent"] = "release_date"
        return (
            "Which property?\nReply with name/area (e.g. Marina).",
            state,
            _property_pick_buttons(props),
        )
    if action == "block":
        state["portfolio_step"] = "pick_property"
        state["portfolio_pending_intent"] = "block_dates"
        return (
            "Which property should I block?\nOr say: _Block Marina from 1 Aug to 15 Aug_",
            state,
            _property_pick_buttons(props),
        )
    if action == "offline":
        state["portfolio_step"] = "pick_property"
        state["portfolio_pending_intent"] = "take_offline"
        return (
            "Which property should go offline?",
            state,
            _property_pick_buttons(props),
        )
    if action == "remove":
        state["portfolio_step"] = "pick_property"
        state["portfolio_pending_intent"] = "remove_listing"
        return (
            "Which listing should I remove from the website?",
            state,
            _property_pick_buttons(props),
        )

    t = (text or "").strip().lower()
    if "block" in t and ("from" in t or "to" in t):
        return (
            "I couldn't parse that block request.\n"
            "Try: _Block Marina from 1 Aug to 15 Aug_",
            {},
            MENU_MANAGE,
        )

    return (
        "Send *hi* for the admin menu.\n\n"
        "Quick actions:\n"
        "• Vacant units\n• Release date\n• Block Marina from 1 Aug to 15 Aug",
        {},
        MENU_HOME,
    )


def _is_greeting(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("hi", "hello", "hey", "menu", "start")


handle_owner_message = handle_admin_portfolio_message
