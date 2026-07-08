import calendar
import datetime
import os

import httpx
from langgraph.graph import StateGraph, END

from app.chatbot.state import ChatState
from app.chatbot.llm_client import call_llm
from app.config import settings


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
        return datetime.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _add_months(d: datetime.date, months: int) -> datetime.date:
    idx = d.month - 1 + months
    year = d.year + idx // 12
    month = idx % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def _months_between(start: datetime.date, end: datetime.date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


async def _quote_and_advance(state: ChatState, months: int | None = None) -> bool:
    """Check availability, compute a duration-aware quote, and move to wait_confirm.

    Returns False if the property is unavailable for the chosen period (the caller
    is responsible for choosing which step to fall back to)."""
    async with httpx.AsyncClient() as client:
        avail_resp = await client.get(
            f"{_api_base()}/api/bookings/check-availability",
            params={
                "property_id": state["selected_property"],
                "start_date": state["start_date"],
                "end_date": state["end_date"],
            },
        )
        if not avail_resp.json().get("available", True):
            return False
        prop_resp = await client.get(f"{_api_base()}/api/properties/{state['selected_property']}")
        prop = prop_resp.json()

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
    else:  # yearly
        units = 1
        unit_label = "year"
        unit_price = float(prop["price_yearly"])

    total = unit_price * units
    state["quote_amount"] = total
    state["reply"] = (
        f"Here is your quote:\n"
        f"Property: {prop['title']}\n"
        f"Dates: {state['start_date']} to {state['end_date']}\n"
        f"Type: {btype} — {units} {unit_label} x AED {unit_price:g}\n"
        f"Total: AED {total:g}\n\n"
        f"Shall I confirm this booking? (yes/no)"
    )
    state["current_step"] = "wait_confirm"
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
        if state.get("booking_id") and step == "wait_accept_full_price":
            state["intent"] = "discount_check"
            return state
        if step in (
            "wait_name", "wait_property", "wait_type",
            "wait_checkin", "wait_checkout",
            "wait_month_start", "wait_months_count", "wait_year_start",
            "wait_discount_amount", "wait_confirm",
        ):
            state["intent"] = "booking"
            return state
        if step in ("wait_unit", "wait_issue_type", "wait_desc"):
            state["intent"] = "maintenance"
            return state

    system_prompt = (
        "Classify the user's message into exactly one of: "
        "booking, maintenance, discount_check, general. "
        "Use discount_check when asking about discount approval status on an existing booking. "
        "Reply with only the single word."
    )
    result = await call_llm(system_prompt, state["incoming_message"])
    intent = result.strip().lower()
    if intent not in ("booking", "maintenance", "discount_check", "general"):
        intent = "general"

    state["intent"] = intent

    if intent == "booking":
        state["current_step"] = "ask_name"
    elif intent == "maintenance":
        state["current_step"] = "ask_unit"

    return state


async def _fetch_properties(client: httpx.AsyncClient):
    resp = await client.get(f"{_api_base()}/api/properties/")
    return resp.json() if resp.status_code == 200 else []


async def _create_booking(client: httpx.AsyncClient, payload: dict):
    return await client.post(f"{_api_base()}/api/bookings/", json=payload)


async def handle_booking(state: ChatState) -> ChatState:
    step = state.get("current_step") or "ask_name"
    msg = state["incoming_message"]
    current_year = datetime.datetime.now().year

    if step == "ask_name":
        state["reply"] = "May I have your full name please?"
        state["current_step"] = "wait_name"
        return state

    if step == "wait_name":
        prompt = "Extract the person's full name from this message. Return ONLY the name."
        name = await call_llm(prompt, msg)
        state["guest_name"] = name.strip()

        async with httpx.AsyncClient() as client:
            props = await _fetch_properties(client)

        prop_list = "\n".join([f"- {p['title']} (AED {p['price_daily']}/day)" for p in props])
        state["reply"] = (
            f"Which property would you like, {state['guest_name']}?\n\n"
            f"Available properties:\n{prop_list}"
        )
        state["current_step"] = "wait_property"
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
            state["reply"] = "Great — what is your check-in date? (e.g. 2026-03-10)"
            state["current_step"] = "wait_checkin"
        else:
            state["reply"] = "Please choose daily, monthly, or yearly."
        return state

    # ---- Daily: explicit check-in / check-out days ----
    if step == "wait_checkin":
        prompt = (
            f"Extract the check-in date from this message and format it as YYYY-MM-DD. "
            f"Assume the year is {current_year} unless specified otherwise. Return ONLY the date."
        )
        parsed = _parse_iso((await call_llm(prompt, msg)).strip())
        if not parsed:
            state["reply"] = "I couldn't read that date. Please use a format like 2026-03-10."
            return state
        state["start_date"] = parsed.isoformat()
        state["reply"] = "And your check-out date?"
        state["current_step"] = "wait_checkout"
        return state

    if step == "wait_checkout":
        prompt = (
            f"Extract the check-out date from this message and format it as YYYY-MM-DD. "
            f"Assume the year is {current_year} unless specified otherwise. Return ONLY the date."
        )
        parsed = _parse_iso((await call_llm(prompt, msg)).strip())
        if not parsed:
            state["reply"] = "I couldn't read that date. Please use a format like 2026-03-15."
            return state
        if parsed <= datetime.date.fromisoformat(state["start_date"]):
            state["reply"] = "Check-out must be after check-in. What is your check-out date?"
            return state
        state["end_date"] = parsed.isoformat()
        if not await _quote_and_advance(state):
            state["reply"] = (
                "Sorry, this property is already booked for those dates. "
                "When would you like to check in?"
            )
            state["current_step"] = "wait_checkin"
        return state

    # ---- Monthly: start month + number of months ----
    if step == "wait_month_start":
        prompt = (
            f"Extract the start month from this message and format it as YYYY-MM-01 "
            f"(always day 01). Assume the year is {current_year} unless specified. Return ONLY the date."
        )
        parsed = _parse_iso((await call_llm(prompt, msg)).strip())
        if not parsed:
            state["reply"] = "I couldn't read that month. Please tell me a month like March 2026."
            return state
        state["start_date"] = parsed.replace(day=1).isoformat()
        state["reply"] = "For how many months would you like to rent?"
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
            state["reply"] = (
                "Sorry, this property is already booked for that period. "
                "Which month would you like to start?"
            )
            state["current_step"] = "wait_month_start"
        return state

    # ---- Yearly: start month, check-out auto-set one year later ----
    if step == "wait_year_start":
        prompt = (
            f"Extract the start month from this message and format it as YYYY-MM-01 "
            f"(always day 01). Assume the year is {current_year} unless specified. Return ONLY the date."
        )
        parsed = _parse_iso((await call_llm(prompt, msg)).strip())
        if not parsed:
            state["reply"] = "I couldn't read that month. Please tell me a month like March 2026."
            return state
        start = parsed.replace(day=1)
        state["start_date"] = start.isoformat()
        state["end_date"] = _add_months(start, 12).isoformat()
        if not await _quote_and_advance(state):
            state["reply"] = (
                "Sorry, this property isn't available for that year. "
                "Which month would you like to start?"
            )
            state["current_step"] = "wait_year_start"
        return state

    if step == "wait_discount_amount":
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
            state["reply"] = "Sure — how much of a discount would you like to request? (amount in AED)"
            state["current_step"] = "wait_discount_amount"
            return state
        if any(w in lower for w in ("yes", "confirm", "ok", "sure")):
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
                confirm_resp = await client.post(
                    f"{_api_base()}/api/bookings/{booking['id']}/guest-confirm"
                )

            if confirm_resp.status_code != 200:
                state["reply"] = "Booking was created but confirmation failed. Please contact support."
            else:
                confirmed = confirm_resp.json()
                state["booking_id"] = confirmed["id"]
                state["reply"] = (
                    f"Booking confirmed!\n"
                    f"Reference: {confirmed['id'][:8].upper()}\n"
                    f"Dates: {confirmed['start_date']} to {confirmed['end_date']}\n"
                    f"Total: AED {confirmed['final_price']}\n\n"
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

    if step == "wait_accept_full_price":
        if any(w in msg.lower() for w in ("yes", "confirm", "ok", "accept", "sure")):
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{_api_base()}/api/bookings/{booking_id}/guest-confirm")
            if resp.status_code != 200:
                state["reply"] = "Could not confirm the booking. Please try again or contact support."
            else:
                booking = resp.json()
                state["reply"] = (
                    f"Booking confirmed at full price!\n"
                    f"Reference: {booking['id'][:8].upper()}\n"
                    f"Total: AED {booking['final_price']}\n\n"
                    f"Please transfer the amount to complete your stay."
                )
            state["current_step"] = None
            state["intent"] = None
        else:
            state["reply"] = "No problem — your booking remains pending if you change your mind."
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
                f"Shall I confirm the booking at full price? (yes/no)"
            )
            state["current_step"] = "wait_accept_full_price"
            state["intent"] = "discount_check"
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
        "Answer briefly and helpfully. If relevant, invite the user to book a property "
        "or report a maintenance issue."
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
    graph.add_node("discount_check", handle_discount_check)
    graph.add_node("general", handle_general)

    graph.set_entry_point("classify_intent")
    graph.add_conditional_edges(
        "classify_intent",
        route_by_intent,
        {
            "booking": "booking",
            "maintenance": "maintenance",
            "discount_check": "discount_check",
            "general": "general",
        },
    )
    graph.add_edge("booking", END)
    graph.add_edge("maintenance", END)
    graph.add_edge("discount_check", END)
    graph.add_edge("general", END)

    return graph.compile()


chatbot_graph = build_graph()
