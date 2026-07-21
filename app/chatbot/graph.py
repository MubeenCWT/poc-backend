import calendar
import datetime
import os
import re

import httpx
from langgraph.graph import StateGraph, END

from app.chatbot.state import ChatState
from app.chatbot.llm_client import call_llm
from app.chatbot.owner_graph import next_release_info
from app.config import settings
from app.database import SessionLocal
from app.models.models import Property


def _api_base() -> str:
    base = (settings.API_BASE_URL or "").strip()
    # Ignore a stale localhost:8000 override — nothing listens there on Railway.
    if base and "localhost:8000" not in base and "127.0.0.1:8000" not in base:
        return base.rstrip("/")
    # On Railway, call our own public domain (always reachable).
    public = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if public:
        return f"https://{public}"
    # Local dev / same container: use the port we're actually bound to.
    return f"http://localhost:{os.environ.get('PORT', '8000')}"


def _parse_iso(value: str):
    try:
        return datetime.date.fromisoformat(value.strip())
    except (ValueError, TypeError):
        return None


async def _parse_user_date(msg: str, current_year: int) -> datetime.date | None:
    """Parse dates from many common formats — not only YYYY-MM-DD."""
    raw = (msg or "").strip()
    if not raw:
        return None

    lower = raw.lower()
    today = datetime.date.today()
    if "next month" in lower:
        return _add_months(today.replace(day=1), 1)
    if "this month" in lower:
        return today.replace(day=1)

    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", raw)
    if iso_match:
        parsed = _parse_iso(iso_match.group(1))
        if parsed:
            return parsed

    for fmt in (
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
        "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
        "%b %d, %Y", "%B %d, %Y",
        "%d %b", "%d %B", "%b %d", "%B %d",
    ):
        try:
            parsed = datetime.datetime.strptime(raw, fmt).date()
            if parsed.year < 100:
                parsed = parsed.replace(year=current_year)
            elif fmt in ("%d %b", "%d %B", "%b %d", "%B %d") and parsed.year == 1900:
                parsed = parsed.replace(year=current_year)
            return parsed
        except ValueError:
            continue

    prompt = (
        f"Convert the user's message to a single date in YYYY-MM-DD format. "
        f"Today is {datetime.date.today().isoformat()}. "
        f"If no year is given, use {current_year}. "
        f"If the message is NOT asking for or giving a date, reply exactly: UNKNOWN. "
        f"Return ONLY the date or UNKNOWN."
    )
    llm_out = (await call_llm(prompt, raw)).strip()
    if not llm_out or llm_out.upper() == "UNKNOWN":
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", llm_out)
    if match:
        return _parse_iso(match.group(0))
    return _parse_iso(llm_out)


RELEASE_QUERY_PHRASES = (
    "when will", "when is", "when does", "when can", "when could",
    "released", "release date", "available again", "free again",
    "next available", "become available", "occupied until", "booked until",
    "guest leave", "check out", "checkout",
)


def _is_release_query(msg: str) -> bool:
    lower = (msg or "").lower()
    if not any(w in lower for w in ("when", "release", "available", "free", "occupied", "booked", "vacant")):
        return False
    return "?" in lower or any(p in lower for p in RELEASE_QUERY_PHRASES)


def _release_info_reply(property_id: str) -> str:
    db = SessionLocal()
    try:
        prop = db.get(Property, property_id)
        if not prop:
            return "I couldn't find that property."
        info = next_release_info(db, prop)
        title = prop.title
        if info["status"] == "available":
            return (
                f"*{title}* is available right now — you can book it for your preferred dates.\n\n"
                f"What check-in date works for you?"
            )
        if info["status"] == "offline":
            return f"*{title}* is currently offline and not accepting new bookings."
        release = info.get("available_from")
        guest = info.get("guest")
        end = info.get("booking_end")
        lines = [f"Here's when *{title}* will be free:"]
        if guest:
            lines.append(f"Current guest: {guest}")
        if end:
            lines.append(f"Current stay ends: {end.strftime('%d %b %Y')}")
        if release:
            lines.append(f"Available for new bookings from: *{release.strftime('%d %b %Y')}*")
        lines.append("\nWant to book from that date? Just tell me your check-in.")
        return "\n".join(lines)
    finally:
        db.close()


def _try_answer_release_query(state: ChatState, msg: str) -> bool:
    """Answer open questions like 'when will it be released?' mid-booking."""
    if not _is_release_query(msg):
        return False
    prop_id = state.get("selected_property")
    if not prop_id:
        state["reply"] = "Which property are you asking about?"
        state["current_step"] = "wait_property"
        state["intent"] = "booking"
        return True
    state["reply"] = _release_info_reply(prop_id)
    return True


def _add_months(d: datetime.date, months: int) -> datetime.date:
    idx = d.month - 1 + months
    year = d.year + idx // 12
    month = idx % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def _months_between(start: datetime.date, end: datetime.date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def _month_bounds(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    start = datetime.date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    return start, datetime.date(year, month, last_day)


def _this_month_bounds(today: datetime.date | None = None) -> tuple[datetime.date, datetime.date]:
    today = today or datetime.date.today()
    start, end = _month_bounds(today.year, today.month)
    if today.day > 1:
        start = today
    return start, end


def _month_label(d: datetime.date) -> str:
    return d.strftime("%B %Y")


async def _check_availability(
    client: httpx.AsyncClient, property_id: str, start_date: str, end_date: str
) -> bool:
    resp = await client.get(
        f"{_api_base()}/api/bookings/check-availability",
        params={
            "property_id": property_id,
            "start_date": start_date,
            "end_date": end_date,
        },
    )
    if resp.status_code != 200:
        return False
    return bool(resp.json().get("available", False))


AVAILABILITY_KEYWORDS = (
    "available", "availability", "free", "vacant", "open",
    "booked", "occupied", "not available", "unavailable",
)
MONTH_WORDS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "month", "this month", "next month",
)


async def _resolve_availability_period(msg: str, current_year: int) -> tuple[datetime.date, datetime.date, str] | None:
    """Parse a month or date range from the user's message."""
    lower = msg.lower()
    today = datetime.date.today()

    if "this month" in lower:
        start, end = _this_month_bounds(today)
        return start, end, _month_label(today)

    if "next month" in lower:
        anchor = _add_months(today.replace(day=1), 1)
        start, end = _month_bounds(anchor.year, anchor.month)
        return start, end, _month_label(anchor)

    prompt = (
        f"Extract the month or date range the user is asking about. "
        f"Return exactly two ISO dates separated by a comma: start,end. "
        f"For a single month use YYYY-MM-01 as start and the last day of that month as end. "
        f"Assume the year is {current_year} unless specified. Today is {today.isoformat()}."
    )
    raw = (await call_llm(prompt, msg)).strip()
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 2:
        return None
    start = _parse_iso(parts[0])
    end = _parse_iso(parts[1])
    if not start or not end:
        return None
    if end < start:
        start, end = end, start
    label = _month_label(start) if start.day == 1 and end.day >= 28 else f"{start.isoformat()} to {end.isoformat()}"
    return start, end, label


def _match_property(props: list, query: str):
    q = query.lower().strip()
    if not q or q == "unknown":
        return None
    for p in props:
        title = (p.get("title") or "").lower()
        area = (p.get("area") or "").lower()
        if q in title or title in q or (area and (q in area or area in q)):
            return p
    return None


def _format_property_details(prop: dict) -> str:
    lines = [
        f"*{prop['title']}*",
        f"Location: {prop.get('area') or '—'}, {prop.get('emirate') or ''}".strip().rstrip(","),
    ]
    if prop.get("bedrooms") is not None:
        lines.append(f"Bedrooms: {prop['bedrooms']}")
    if prop.get("bathrooms") is not None:
        lines.append(f"Bathrooms: {prop['bathrooms']}")
    if prop.get("max_guests") is not None:
        lines.append(f"Max guests: {prop['max_guests']}")
    prices = []
    if prop.get("price_daily"):
        prices.append(f"Daily: AED {float(prop['price_daily']):g}")
    if prop.get("price_monthly"):
        prices.append(f"Monthly: AED {float(prop['price_monthly']):g}")
    if prop.get("price_yearly"):
        prices.append(f"Yearly: AED {float(prop['price_yearly']):g}")
    if prices:
        lines.append("Rates — " + " · ".join(prices))
    if prop.get("description"):
        lines.append(prop["description"][:280])
    return "\n".join(lines)


INQUIRE_PHRASES = (
    "inquire", "enquire", "inquiry", "enquiry", "tell me about",
    "details about", "info about", "information about", "want to know about",
    "i want to inquire", "interested in",
)

SWITCH_PROPERTY_PHRASES = (
    "different property", "another property", "other property", "change property",
    "not this", "don't want this", "dont want this", "instead", "rather",
    "i want a different", "switch to", "look at",
)

CANCEL_PHRASES = (
    "cancel", "stop", "nevermind", "never mind", "start over", "forget it",
    "don't want to book", "dont want to book",
)


async def _try_flexible_booking_redirect(state: ChatState, msg: str) -> bool:
    """Handle mid-flow changes (new property, cancel, inquire) instead of forcing the current step.

    Returns True if the message was handled and the normal step logic should be skipped.
    """
    lower = msg.lower()
    step = state.get("current_step") or ""

    # Only apply during an active booking flow
    booking_steps = {
        "ask_property", "ask_type", "wait_property", "wait_type",
        "wait_checkin", "wait_checkout", "wait_month_start", "wait_months_count",
        "wait_year_start", "wait_name_before_confirm", "wait_discount_amount", "wait_confirm",
    }
    if step not in booking_steps and state.get("intent") != "booking":
        return False

    if any(p in lower for p in CANCEL_PHRASES):
        state["reply"] = "No problem — I've cancelled this booking flow. How else can I help?"
        state["intent"] = None
        state["current_step"] = None
        state["selected_property"] = None
        state["start_date"] = None
        state["end_date"] = None
        state["booking_type"] = None
        state["guest_name"] = None
        return True

    # Detect inquire / switch property even while waiting for dates etc.
    wants_switch = any(p in lower for p in SWITCH_PROPERTY_PHRASES) or any(p in lower for p in INQUIRE_PHRASES)
    # Also: user names another property while we're mid-date ("no i want burj instead")
    if not wants_switch and step in (
        "wait_checkin", "wait_checkout", "wait_month_start", "wait_months_count",
        "wait_year_start", "wait_type", "wait_confirm", "wait_name_before_confirm",
    ):
        if _is_release_query(msg):
            return False
        looks_like_answer = bool(
            re.search(r"\d{4}-\d{2}-\d{2}", lower)
            or re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", lower)
            or re.fullmatch(r"\d+", msg.strip())
            or any(w in lower for w in ("daily", "monthly", "yearly", "day", "night", "month", "year", "yes", "no", "ok", "sure", "confirm"))
        )
        if not looks_like_answer and len(msg.strip()) > 8:
            async with httpx.AsyncClient() as client:
                props = await _fetch_properties(client)
            prompt = (
                "Does the user want a DIFFERENT property than the current one, "
                "or are they answering the booking question (dates/type/name)? "
                "Reply ONLY switch or continue."
            )
            verdict = (await call_llm(prompt, msg)).strip().lower()
            if "switch" in verdict:
                prop_prompt = "Extract the property name or area. Return ONLY the name/area, or UNKNOWN."
                prop_query = await call_llm(prop_prompt, msg)
                matched_early = _match_property(props, prop_query)
                if matched_early:
                    state["selected_property"] = matched_early["id"]
                    state["start_date"] = None
                    state["end_date"] = None
                    state["booking_type"] = None
                    details = _format_property_details(matched_early)
                    state["reply"] = (
                        f"Sure — switching to that one.\n\n{details}\n\n"
                        f"Would you like to book it? Choose daily, monthly, or yearly — "
                        f"or ask me anything else about it."
                    )
                    state["current_step"] = "wait_type"
                    state["intent"] = "booking"
                    return True

    if wants_switch or any(p in lower for p in INQUIRE_PHRASES):
        async with httpx.AsyncClient() as client:
            props = await _fetch_properties(client)
        prompt = "Extract the property name or area the user means. Return ONLY the name/area, or UNKNOWN."
        prop_query = await call_llm(prompt, msg)
        matched = _match_property(props, prop_query)
        if matched:
            state["selected_property"] = matched["id"]
            state["start_date"] = None
            state["end_date"] = None
            state["booking_type"] = None
            details = _format_property_details(matched)
            state["reply"] = (
                f"Here's what I have:\n\n{details}\n\n"
                f"Would you like to book this stay? Say daily, monthly, or yearly to continue — "
                f"or ask about availability / another property."
            )
            state["current_step"] = "wait_type"
            state["intent"] = "booking"
            return True
        prop_list = "\n".join(f"- {p['title']}" for p in props)
        state["reply"] = f"Which property did you mean?\n\n{prop_list}"
        state["current_step"] = "wait_property"
        state["intent"] = "booking"
        return True

    return False


# Words that must never be accepted as a guest name.
_INVALID_NAME_WORDS = {
    "yes", "no", "ok", "okay", "sure", "confirm", "y", "n", "yeah", "yep", "nah",
    "book", "booking", "hi", "hello", "thanks", "thank you",
}


def _is_valid_guest_name(name: str | None) -> bool:
    if not name:
        return False
    cleaned = " ".join(name.strip().split())
    if len(cleaned) < 2:
        return False
    lower = cleaned.lower()
    if lower in _INVALID_NAME_WORDS or lower == "unknown":
        return False
    if cleaned.isdigit():
        return False
    return any(c.isalpha() for c in cleaned)


async def _extract_guest_name(msg: str) -> str:
    prompt = (
        "Extract the person's full name from this message. "
        "Return ONLY the first and last name if clearly provided as the user's own name. "
        "If the message is a yes/no, a date, a place, a property, or anything that is not "
        "clearly a personal name being given, return UNKNOWN."
    )
    raw = (await call_llm(prompt, msg)).strip()
    if not raw or raw.upper() == "UNKNOWN" or "unknown" in raw.lower():
        return "UNKNOWN"
    return raw


def _build_quote_reply(state: ChatState, prop: dict, months: int | None = None) -> str:
    start = datetime.date.fromisoformat(state["start_date"])
    end = datetime.date.fromisoformat(state["end_date"])
    btype = state["booking_type"]

    if btype == "daily":
        units = max((end - start).days, 1)
        unit_label = "night" if units == 1 else "nights"
        unit_price = float(prop["price_daily"])
    elif btype == "monthly":
        units = months if months else max(_months_between(start, end), 1)
        unit_label = "month" if units == 1 else "months"
        unit_price = float(prop["price_monthly"])
    else:
        units = 1
        unit_label = "year"
        unit_price = float(prop["price_yearly"])

    total = unit_price * units
    state["quote_amount"] = total
    return (
        f"Here is your quote:\n"
        f"Property: {prop['title']}\n"
        f"Dates: {state['start_date']} to {state['end_date']}\n"
        f"Type: {btype} — {units} {unit_label} x AED {unit_price:g}\n"
        f"Total: AED {total:g}"
    )


async def _quote_and_advance(state: ChatState, months: int | None = None) -> bool:
    """Check availability, compute quote, then ALWAYS ask for name before confirmation."""
    async with httpx.AsyncClient() as client:
        if not await _check_availability(
            client,
            state["selected_property"],
            state["start_date"],
            state["end_date"],
        ):
            return False
        prop_resp = await client.get(f"{_api_base()}/api/properties/{state['selected_property']}")
        prop = prop_resp.json()

    quote = _build_quote_reply(state, prop, months=months)
    # Strict rule: always collect name for THIS booking — never reuse a stale session name.
    state["guest_name"] = None
    state["reply"] = (
        f"{quote}\n\n"
        f"Before I can confirm, may I have your full name please?"
    )
    state["current_step"] = "wait_name_before_confirm"
    return True


# The bot never proactively offers a discount — it only reacts if the tenant asks.
DISCOUNT_KEYWORDS = (
    "discount", "cheaper", "lower", "reduce", "less", "deal",
    "offer", "negotiat", "concession", "best price",
)


async def classify_intent(state: ChatState) -> ChatState:
    if state.get("intent"):
        return state

    # Mid-flow steps should stay in the current intent
    step = state.get("current_step") or ""
    if step.startswith("wait_") or step.startswith("ask_"):
        if state.get("booking_id") and step in ("wait_accept_full_price", "wait_counter_response"):
            state["intent"] = "discount_check"
            return state
        if step in ("wait_avail_property", "wait_avail_period"):
            state["intent"] = "availability"
            return state
        if step in (
            "ask_property", "ask_type", "wait_property", "wait_type",
            "wait_checkin", "wait_checkout",
            "wait_month_start", "wait_months_count", "wait_year_start",
            "wait_name_before_confirm", "wait_discount_amount", "wait_confirm",
            "show_property_details",
        ):
            state["intent"] = "booking"
            return state
        if step in ("wait_unit", "wait_issue_type", "wait_desc"):
            state["intent"] = "maintenance"
            return state

    msg = state.get("incoming_message") or ""
    lower = msg.lower()

    # Property inquire from website deep-link or freeform
    if any(p in lower for p in INQUIRE_PHRASES):
        state["intent"] = "booking"
        state["current_step"] = "show_property_details"
        return state

    if any(k in lower for k in AVAILABILITY_KEYWORDS) or _is_release_query(msg) or (
        any(m in lower for m in MONTH_WORDS) and any(w in lower for w in ("available", "free", "book", "booked"))
    ):
        state["intent"] = "availability"
        return state

    system_prompt = (
        "Classify the user's message into exactly one of: "
        "booking, maintenance, discount_check, availability, general. "
        "Use booking if they want to inquire about or book a specific property. "
        "Use availability when asking if a property is free for dates or a month. "
        "Use discount_check when asking about discount approval status on an existing booking. "
        "Reply with only the single word."
    )
    result = await call_llm(system_prompt, state["incoming_message"])
    intent = result.strip().lower()
    if intent not in ("booking", "maintenance", "discount_check", "availability", "general"):
        intent = "general"

    state["intent"] = intent

    if intent == "booking":
        # Fresh booking — don't carry over an old name/dates from this WhatsApp session
        state["guest_name"] = None
        state["booking_id"] = None
        state["quote_amount"] = None
        if state.get("selected_property"):
            state["current_step"] = "ask_type"
        else:
            state["current_step"] = "ask_property"
    elif intent == "maintenance":
        state["current_step"] = "ask_unit"

    return state


async def _fetch_properties(client: httpx.AsyncClient):
    resp = await client.get(f"{_api_base()}/api/properties/")
    return resp.json() if resp.status_code == 200 else []


async def _create_booking(client: httpx.AsyncClient, payload: dict):
    return await client.post(f"{_api_base()}/api/bookings/", json=payload)


async def handle_booking(state: ChatState) -> ChatState:
    step = state.get("current_step") or "ask_property"
    msg = state["incoming_message"]
    current_year = datetime.datetime.now().year
    state["reply_buttons"] = None

    if _try_answer_release_query(state, msg):
        return state

    # Allow switching property / cancelling / inquiring mid-flow
    if await _try_flexible_booking_redirect(state, msg):
        return state

    if step == "show_property_details":
        async with httpx.AsyncClient() as client:
            props = await _fetch_properties(client)
        prompt = "Extract the property name or area. Return ONLY the name/area, or UNKNOWN."
        prop_query = await call_llm(prompt, msg)
        matched = _match_property(props, prop_query)
        if not matched and state.get("selected_property"):
            matched = next((p for p in props if p["id"] == state["selected_property"]), None)
        if not matched:
            prop_list = "\n".join(f"- {p['title']}" for p in props)
            state["reply"] = f"Which property are you asking about?\n\n{prop_list}"
            state["current_step"] = "wait_property"
            return state
        state["selected_property"] = matched["id"]
        details = _format_property_details(matched)
        state["reply"] = (
            f"Here's what I have:\n\n{details}\n\n"
            f"Would you like to book it? Say daily, monthly, or yearly — "
            f"or ask about another property / availability."
        )
        state["current_step"] = "wait_type"
        return state

    if step == "ask_property":
        async with httpx.AsyncClient() as client:
            props = await _fetch_properties(client)

        prop_list = "\n".join([f"- {p['title']} (AED {p['price_daily']}/day)" for p in props])
        state["reply"] = f"Which property would you like?\n\nAvailable properties:\n{prop_list}"
        state["current_step"] = "wait_property"
        return state

    if step == "ask_type":
        async with httpx.AsyncClient() as client:
            prop_resp = await client.get(f"{_api_base()}/api/properties/{state['selected_property']}")
            matched = prop_resp.json() if prop_resp.status_code == 200 else None

        if not matched:
            state["current_step"] = "ask_property"
            state["reply"] = "Which property would you like to book?"
            return state

        state["reply"] = (
            f"Would you like a daily, monthly, or yearly rental for {matched['title']}?\n"
            f"- Daily: AED {matched['price_daily']:g}/night\n"
            f"- Monthly: AED {matched['price_monthly']:g}/month\n"
            f"- Yearly: AED {matched['price_yearly']:g}/year"
        )
        state["current_step"] = "wait_type"
        return state

    if step == "wait_property":
        prompt = "Extract the property name or area the user is referring to. Return ONLY the name/area."
        prop_query = await call_llm(prompt, msg)

        async with httpx.AsyncClient() as client:
            props = await _fetch_properties(client)

        matched = next((p for p in props if prop_query.lower() in p["title"].lower()), None)
        if not matched:
            state["reply"] = "I couldn't find that property. Which one would you like?"
            return state

        state["selected_property"] = matched["id"]
        state["reply"] = (
            f"Would you like a daily, monthly, or yearly rental?\n"
            f"- Daily: AED {matched['price_daily']:g}/night\n"
            f"- Monthly: AED {matched['price_monthly']:g}/month\n"
            f"- Yearly: AED {matched['price_yearly']:g}/year"
        )
        state["current_step"] = "wait_type"
        return state

    # Ask rental type first, then collect the dates that make sense for that type.
    if step == "wait_type":
        lower = msg.lower()
        if "month" in lower:
            state["booking_type"] = "monthly"
            state["reply"] = "Great — which month would you like to start? (e.g. March 2026)"
            state["current_step"] = "wait_month_start"
        elif "year" in lower:
            state["booking_type"] = "yearly"
            state["reply"] = (
                "Great — which month would you like your 1-year lease to start? (e.g. March 2026)\n"
                "I'll set the check-out to one year later automatically."
            )
            state["current_step"] = "wait_year_start"
        elif "dai" in lower or "day" in lower or "night" in lower:
            state["booking_type"] = "daily"
            state["reply"] = "Great — what is your check-in date? (e.g. 22 July or 2026-07-22)"
            state["current_step"] = "wait_checkin"
        else:
            state["reply"] = "Please choose daily, monthly, or yearly."
        return state

    # ---- Daily: explicit check-in / check-out days ----
    if step == "wait_checkin":
        if _try_answer_release_query(state, msg):
            return state
        parsed = await _parse_user_date(msg, current_year)
        if not parsed:
            state["reply"] = (
                "I couldn't quite catch that date. You can say things like "
                "22 July, 22/07/2026, or 2026-07-22.\n"
                "Or ask: *when will this property be released?*"
            )
            return state
        state["start_date"] = parsed.isoformat()
        state["reply"] = "And your check-out date?"
        state["current_step"] = "wait_checkout"
        return state

    if step == "wait_checkout":
        if _try_answer_release_query(state, msg):
            return state
        parsed = await _parse_user_date(msg, current_year)
        if not parsed:
            state["reply"] = (
                "I couldn't quite catch that date. You can say things like "
                "25 July, 25/07/2026, or 2026-07-25.\n"
                "Or ask: *when will this property be released?*"
            )
            return state
        if parsed <= datetime.date.fromisoformat(state["start_date"]):
            state["reply"] = "Check-out must be after check-in. What is your check-out date?"
            return state
        state["end_date"] = parsed.isoformat()
        if not await _quote_and_advance(state):
            start = datetime.date.fromisoformat(state["start_date"])
            end = datetime.date.fromisoformat(state["end_date"])
            state["reply"] = (
                f"Sorry, this property is not available from {start.strftime('%d %b %Y')} "
                f"to {end.strftime('%d %b %Y')}.\n\n"
                f"{_release_info_reply(state['selected_property'])}"
            )
            state["current_step"] = "wait_checkin"
        return state

    # ---- Monthly: start month + number of months ----
    if step == "wait_month_start":
        if _try_answer_release_query(state, msg):
            return state
        parsed = await _parse_user_date(msg, current_year)
        if not parsed:
            state["reply"] = (
                "Which month would you like to start? "
                "You can say March 2026, next month, or 01/03/2026."
            )
            return state
        start = parsed.replace(day=1)
        state["start_date"] = start.isoformat()

        async with httpx.AsyncClient() as client:
            prop_resp = await client.get(f"{_api_base()}/api/properties/{state['selected_property']}")
            prop = prop_resp.json()
            month_end = _add_months(start, 1)
            available = await _check_availability(
                client, state["selected_property"], start.isoformat(), month_end.isoformat()
            )

        month_name = _month_label(start)
        if not available:
            state["reply"] = (
                f"Sorry, {prop['title']} is not available in {month_name}.\n\n"
                f"{_release_info_reply(state['selected_property'])}"
            )
            return state

        state["reply"] = (
            f"Good news — {prop['title']} is available in {month_name}. "
            f"For how many months would you like to rent?"
        )
        state["current_step"] = "wait_months_count"
        return state

    if step == "wait_months_count":
        prompt = "Extract the number of months from this message. Return ONLY the integer."
        num_str = await call_llm(prompt, msg)
        try:
            months = int(float(num_str.strip().replace(",", "")))
        except ValueError:
            months = 0
        if months < 1:
            state["reply"] = "Please tell me how many months (e.g. 3)."
            return state
        start = datetime.date.fromisoformat(state["start_date"])
        state["end_date"] = _add_months(start, months).isoformat()
        state["months_count"] = months
        if not await _quote_and_advance(state, months=months):
            start = datetime.date.fromisoformat(state["start_date"])
            state["reply"] = (
                f"Sorry, this property is not available for that {months}-month period "
                f"starting {_month_label(start)}.\n\n"
                f"{_release_info_reply(state['selected_property'])}"
            )
            state["current_step"] = "wait_month_start"
        return state

    # ---- Yearly: start month, check-out auto-set one year later ----
    if step == "wait_year_start":
        if _try_answer_release_query(state, msg):
            return state
        parsed = await _parse_user_date(msg, current_year)
        if not parsed:
            state["reply"] = (
                "Which month should the 1-year lease start? "
                "You can say March 2026, next month, or 01/03/2026."
            )
            return state
        start = parsed.replace(day=1)
        state["start_date"] = start.isoformat()
        state["end_date"] = _add_months(start, 12).isoformat()
        if not await _quote_and_advance(state):
            state["reply"] = (
                f"Sorry, this property is not available for a year starting {_month_label(start)}.\n\n"
                f"{_release_info_reply(state['selected_property'])}"
            )
            state["current_step"] = "wait_year_start"
        return state

    if step == "wait_name_before_confirm":
        name = await _extract_guest_name(msg)
        if not _is_valid_guest_name(name):
            state["reply"] = (
                "I need your full name before I can confirm the booking. "
                "Please reply with your first and last name."
            )
            return state

        state["guest_name"] = " ".join(name.split())
        async with httpx.AsyncClient() as client:
            prop_resp = await client.get(f"{_api_base()}/api/properties/{state['selected_property']}")
            prop = prop_resp.json()

        months = state.get("months_count")
        quote = _build_quote_reply(state, prop, months=months)
        state["reply"] = (
            f"Thank you, {state['guest_name']}.\n\n"
            f"{quote}\n\n"
            f"Shall I confirm this booking? (yes/no)"
        )
        state["current_step"] = "wait_confirm"
        return state

    if step == "wait_discount_amount":
        if not _is_valid_guest_name(state.get("guest_name")):
            state["reply"] = (
                "I need your full name before I can submit a discount request. "
                "Please reply with your first and last name."
            )
            state["current_step"] = "wait_name_before_confirm"
            return state

        prompt = "Extract the discount amount in AED from this message. Return ONLY the number."
        amount_str = await call_llm(prompt, msg)
        try:
            discount_amount = float(amount_str.strip().replace(",", ""))
        except ValueError:
            state["reply"] = "Please enter a valid discount amount in AED."
            return state

        payload = {
            "property_id": state["selected_property"],
            "guest_name": state["guest_name"],
            "guest_phone": state.get("phone") or "web",
            "booking_type": state["booking_type"],
            "start_date": state["start_date"],
            "end_date": state["end_date"],
            "discount_requested": True,
            "discount_amount": discount_amount,
            "source": "chatbot",
        }

        async with httpx.AsyncClient() as client:
            resp = await _create_booking(client, payload)

        if resp.status_code != 200:
            detail = resp.json().get("detail", "the property is not available for those dates.")
            state["reply"] = f"Sorry, {detail} Let's try again."
            state["intent"] = None
            state["current_step"] = None
            return state

        booking = resp.json()
        state["booking_id"] = booking["id"]
        state["reply"] = (
            f"Your booking is pending while we review your discount request.\n"
            f"Reference: {booking['id'][:8].upper()}\n"
            f"Dates: {booking['start_date']} to {booking['end_date']}\n"
            f"Base price: AED {booking['base_price']}\n"
            f"Discount requested: AED {discount_amount}\n\n"
            f"Ask me anytime: \"Is my discount approved?\""
        )
        state["intent"] = None
        state["current_step"] = None
        return state

    if step == "wait_confirm":
        lower = msg.lower()
        # Discount is never offered by the bot — only handled if the tenant asks.
        if any(k in lower for k in DISCOUNT_KEYWORDS):
            if not _is_valid_guest_name(state.get("guest_name")):
                state["reply"] = (
                    "Before we continue, may I have your full name please?"
                )
                state["current_step"] = "wait_name_before_confirm"
                return state
            state["reply"] = "Sure — how much of a discount would you like to request? (amount in AED)"
            state["current_step"] = "wait_discount_amount"
            return state
        if any(w in lower for w in ("yes", "confirm", "ok", "sure")):
            if not _is_valid_guest_name(state.get("guest_name")):
                state["reply"] = (
                    "I can't confirm without your full name. "
                    "Please reply with your first and last name."
                )
                state["current_step"] = "wait_name_before_confirm"
                return state

            payload = {
                "property_id": state["selected_property"],
                "guest_name": state["guest_name"],
                "guest_phone": state.get("phone") or "web",
                "booking_type": state["booking_type"],
                "start_date": state["start_date"],
                "end_date": state["end_date"],
                "discount_requested": False,
                "source": "chatbot",
            }

            async with httpx.AsyncClient() as client:
                resp = await _create_booking(client, payload)
                if resp.status_code != 200:
                    detail = resp.json().get("detail", "the property is not available for those dates.")
                    state["reply"] = f"Sorry, {detail} Let's try again."
                    state["intent"] = None
                    state["current_step"] = None
                    return state

                booking = resp.json()
                state["booking_id"] = booking["id"]
                state["reply"] = (
                    f"Your booking is reserved!\n"
                    f"Reference: {booking['id'][:8].upper()}\n"
                    f"Dates: {booking['start_date']} to {booking['end_date']}\n"
                    f"Total: AED {booking['base_price']}\n\n"
                    f"We'll confirm once payment is received. "
                    f"Please transfer the amount to complete your stay."
                )
            state["intent"] = None
            state["current_step"] = None
        else:
            state["reply"] = "Booking cancelled. How else can I help you?"
            state["intent"] = None
            state["current_step"] = None
        return state

    return state


async def handle_availability(state: ChatState) -> ChatState:
    """Answer whether a specific property is free for a month or date range."""
    step = state.get("current_step")
    msg = state["incoming_message"]
    current_year = datetime.datetime.now().year

    if _is_release_query(msg) and state.get("selected_property"):
        state["reply"] = _release_info_reply(state["selected_property"])
        return state

    async with httpx.AsyncClient() as client:
        props = await _fetch_properties(client)

    if step == "wait_avail_property":
        prompt = "Extract the property name or area the user is referring to. Return ONLY the name/area."
        prop_query = await call_llm(prompt, msg)
        matched = _match_property(props, prop_query)
        if not matched:
            prop_list = "\n".join([f"- {p['title']}" for p in props])
            state["reply"] = f"I couldn't find that property. Which one?\n\n{prop_list}"
            return state
        state["selected_property"] = matched["id"]
        state["reply"] = "Which month or dates should I check? (e.g. this month, March 2026)"
        state["current_step"] = "wait_avail_period"
        return state

    if step == "wait_avail_period":
        matched = next((p for p in props if p["id"] == state.get("selected_property")), None)
        if not matched:
            state["current_step"] = "wait_avail_property"
            state["reply"] = "Which property are you asking about?"
            return state
        if _is_release_query(msg):
            state["reply"] = _release_info_reply(matched["id"])
            return state
        period = await _resolve_availability_period(msg, current_year)
        if not period:
            state["reply"] = (
                "I couldn't read those dates. Try e.g. this month, next month, March 2026, "
                "or ask *when will it be released?*"
            )
            return state
        start, end, label = period
        async with httpx.AsyncClient() as client:
            available = await _check_availability(
                client, matched["id"], start.isoformat(), end.isoformat()
            )
        if available:
            state["reply"] = (
                f"Yes — {matched['title']} is available in {label}.\n"
                f"Say 'book' if you'd like to reserve it."
            )
        else:
            state["reply"] = (
                f"No — {matched['title']} is not available in {label}.\n\n"
                f"{_release_info_reply(matched['id'])}"
            )
        state["intent"] = None
        state["current_step"] = None
        return state

    # Fresh availability question
    if state.get("selected_property"):
        matched = next((p for p in props if p["id"] == state["selected_property"]), None)
    else:
        prompt = (
            "Extract the property name or area from this message. "
            "Return ONLY the name/area, or UNKNOWN if not mentioned."
        )
        prop_query = await call_llm(prompt, msg)
        matched = _match_property(props, prop_query)

    if not matched:
        prop_list = "\n".join([f"- {p['title']}" for p in props])
        state["reply"] = f"Which property should I check?\n\n{prop_list}"
        state["current_step"] = "wait_avail_property"
        state["intent"] = "availability"
        return state

    state["selected_property"] = matched["id"]
    period = await _resolve_availability_period(msg, current_year)

    if not period:
        if _is_release_query(msg):
            state["reply"] = _release_info_reply(matched["id"])
            return state
        state["reply"] = (
            f"Which month or dates should I check for {matched['title']}?\n"
            f"(e.g. this month, next month, March 2026 — or ask when it will be released)"
        )
        state["current_step"] = "wait_avail_period"
        state["intent"] = "availability"
        return state

    start, end, label = period
    async with httpx.AsyncClient() as client:
        available = await _check_availability(
            client, matched["id"], start.isoformat(), end.isoformat()
        )

    if available:
        state["reply"] = (
            f"Yes — {matched['title']} is available in {label}.\n"
            f"Say 'book' if you'd like to reserve it."
        )
    else:
        state["reply"] = (
            f"No — {matched['title']} is not available in {label}.\n\n"
            f"{_release_info_reply(matched['id'])}"
        )

    state["intent"] = None
    state["current_step"] = None
    return state


async def handle_maintenance(state: ChatState) -> ChatState:
    step = state.get("current_step") or "ask_unit"
    msg = state["incoming_message"]

    if step == "ask_unit":
        state["reply"] = "Which unit/property are you in?"
        state["current_step"] = "wait_unit"
        return state

    if step == "wait_unit":
        prompt = "Extract the property name or area the user is referring to. Return ONLY the name/area."
        prop_query = await call_llm(prompt, msg)

        async with httpx.AsyncClient() as client:
            props = await _fetch_properties(client)

        matched = next((p for p in props if prop_query.lower() in p["title"].lower()), None)
        if not matched:
            state["reply"] = "I couldn't find that property. Which one are you in?"
            return state

        state["unit_id"] = matched["id"]
        state["reply"] = "What type of issue is it? (plumbing, electrical, AC, cleaning, or other)"
        state["current_step"] = "wait_issue_type"
        return state

    if step == "wait_issue_type":
        type_str = msg.lower()
        if "plumb" in type_str:
            state["issue_type"] = "plumbing"
        elif "elect" in type_str:
            state["issue_type"] = "electrical"
        elif "ac" in type_str:
            state["issue_type"] = "AC"
        elif "clean" in type_str:
            state["issue_type"] = "cleaning"
        else:
            state["issue_type"] = "other"

        state["reply"] = "Could you please describe the issue in a little more detail?"
        state["current_step"] = "wait_desc"
        return state

    if step == "wait_desc":
        state["issue_description"] = msg
        payload = {
            "property_id": state["unit_id"],
            "requested_by": state.get("guest_name") or state.get("phone") or "Web Guest",
            "issue_type": state["issue_type"],
            "description": state["issue_description"],
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{_api_base()}/api/maintenance/", json=payload)
            data = resp.json()

        state["reply"] = (
            f"Your {state['issue_type']} request has been logged "
            f"(Ref: {data['request_id'][:8].upper()}).\n"
            f"Our team has been notified and will arrange this for you shortly."
        )
        state["intent"] = None
        state["current_step"] = None
        return state

    return state


async def handle_discount_check(state: ChatState) -> ChatState:
    step = state.get("current_step")
    msg = state["incoming_message"]
    booking_id = state.get("booking_id")
    state["reply_buttons"] = None

    if step == "wait_counter_response":
        lower = msg.lower()
        accept = any(w in lower for w in ("yes", "accept", "ok", "sure", "deal", "confirm"))
        decline = any(w in lower for w in ("no", "decline", "reject", "cancel", "pass"))
        if not accept and not decline:
            state["reply"] = "Please accept or decline the counter-offer (yes/no)."
            if booking_id:
                state["reply_buttons"] = [
                    {"id": f"offer_yes_{booking_id}", "title": "Accept offer"},
                    {"id": f"offer_no_{booking_id}", "title": "Decline"},
                ]
            return state
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_api_base()}/api/bookings/{booking_id}/counter-response",
                json={"accept": accept},
            )
        if resp.status_code != 200:
            state["reply"] = "Could not process that response. Please try again or contact support."
        else:
            booking = resp.json()
            if accept:
                state["reply"] = (
                    f"Perfect — offer accepted!\n"
                    f"Booking {booking['id'][:8].upper()} is CONFIRMED.\n"
                    f"Total: AED {booking['final_price']}.\n"
                    f"Please proceed with payment."
                )
            else:
                state["reply"] = (
                    "No problem — we've cancelled that booking and released the dates. "
                    "Message us anytime to book again."
                )
        state["current_step"] = None
        state["intent"] = None
        return state

    if step == "wait_accept_full_price":
        if any(w in msg.lower() for w in ("yes", "confirm", "ok", "accept", "sure")):
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{_api_base()}/api/bookings/{booking_id}/guest-confirm")
            if resp.status_code != 200:
                state["reply"] = "Could not reserve the booking. Please try again or contact support."
            else:
                booking = resp.json()
                state["reply"] = (
                    f"Thanks! Your booking at full price is reserved.\n"
                    f"Reference: {booking['id'][:8].upper()}\n"
                    f"Total: AED {booking['final_price'] or booking['base_price']}\n\n"
                    f"We'll confirm once payment is received."
                )
            state["current_step"] = None
            state["intent"] = None
        else:
            # Decline full price → cancel
            async with httpx.AsyncClient() as client:
                # Soft cancel via counter-response path isn't right; use guest message
                pass
            state["reply"] = "Okay — we won't confirm at full price. Your request stays on hold; say 'book' anytime to start fresh."
            state["current_step"] = None
            state["intent"] = None
        return state

    if not booking_id:
        state["reply"] = (
            "I don't have a booking on file for this chat. "
            "Start a new booking, or complete one first, then ask about your discount."
        )
        state["intent"] = None
        return state

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{_api_base()}/api/bookings/{booking_id}")

    if resp.status_code != 200:
        state["reply"] = "I couldn't find your booking. Please start a new booking."
        state["intent"] = None
        return state

    booking = resp.json()

    if booking["discount_status"] == "pending":
        state["reply"] = (
            "Your discount request is still being reviewed by our team. "
            "I'll let you know once it's decided — check back shortly."
        )
    elif booking["discount_status"] == "countered":
        state["reply"] = (
            f"We sent you a counter-offer on booking {booking['id'][:8].upper()}. "
            f"Would you like to accept it?"
        )
        state["current_step"] = "wait_counter_response"
        state["intent"] = "discount_check"
        state["reply_buttons"] = [
            {"id": f"offer_yes_{booking_id}", "title": "Accept offer"},
            {"id": f"offer_no_{booking_id}", "title": "Decline"},
        ]
        return state
    elif booking["discount_status"] == "approved":
        state["reply"] = (
            f"Good news — your discount was approved!\n"
            f"Booking confirmed at AED {booking['final_price']} "
            f"(saved AED {booking['discount_amount']} off AED {booking['base_price']})."
        )
    elif booking["discount_status"] == "rejected":
        if booking["status"] == "confirmed":
            state["reply"] = f"Your booking is confirmed at AED {booking['final_price']}."
        else:
            state["reply"] = (
                f"Your discount wasn't approved. The full price is AED {booking['base_price']}.\n"
                f"Shall I confirm the booking at full price?"
            )
            state["current_step"] = "wait_accept_full_price"
            state["intent"] = "discount_check"
            state["reply_buttons"] = [
                {"id": f"full_yes_{booking_id}", "title": "Yes, full price"},
                {"id": f"full_no_{booking_id}", "title": "No thanks"},
            ]
            return state
    else:
        if booking["status"] == "confirmed":
            state["reply"] = f"Your booking is confirmed at AED {booking['final_price']}."
        else:
            state["reply"] = (
                f"Your booking is pending at AED {booking['base_price']}. "
                f"Say yes to confirm at full price."
            )
            state["current_step"] = "wait_accept_full_price"
            state["intent"] = "discount_check"
            return state

    state["intent"] = None
    return state


async def handle_general(state: ChatState) -> ChatState:
    system_prompt = (
        "You are a friendly web assistant for a UAE property booking platform. "
        "Answer briefly and helpfully. Do NOT guess whether a property is available — "
        "tell the user to ask e.g. 'Is Marina View available this month?' and you will check. "
        "If relevant, invite the user to book a property or report a maintenance issue."
    )
    state["reply"] = await call_llm(system_prompt, state["incoming_message"])
    state["intent"] = None
    return state


def route_by_intent(state: ChatState) -> str:
    return state.get("intent", "general")


def build_graph():
    graph = StateGraph(ChatState)
    graph.add_node("classify_intent", classify_intent)
    graph.add_node("booking", handle_booking)
    graph.add_node("maintenance", handle_maintenance)
    graph.add_node("availability", handle_availability)
    graph.add_node("discount_check", handle_discount_check)
    graph.add_node("general", handle_general)

    graph.set_entry_point("classify_intent")
    graph.add_conditional_edges(
        "classify_intent",
        route_by_intent,
        {
            "booking": "booking",
            "maintenance": "maintenance",
            "availability": "availability",
            "discount_check": "discount_check",
            "general": "general",
        },
    )
    graph.add_edge("booking", END)
    graph.add_edge("maintenance", END)
    graph.add_edge("availability", END)
    graph.add_edge("discount_check", END)
    graph.add_edge("general", END)

    return graph.compile()


chatbot_graph = build_graph()
