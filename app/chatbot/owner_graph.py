"""WhatsApp portfolio assistant for admin — vacant units, release dates, blocks, offline."""
import datetime
import json
import re

from sqlalchemy.orm import Session

from app.chatbot.llm_client import call_llm
from app.models.models import Property
from app.services.owner_portfolio import (
    block_property_dates,
    bring_property_online,
    match_portfolio_property,
    next_release_info,
    portfolio_properties,
    portfolio_summary,
    take_property_offline,
)

_ADMIN_PORTFOLIO_SYSTEM = """You are UAE Stays admin assistant. The admin manages all rental apartments via WhatsApp.

Classify their message into ONE intent and extract fields as JSON only (no markdown):
{
  "intent": "portfolio" | "release_date" | "block_dates" | "take_offline" | "bring_online" | "help" | "unknown",
  "property_query": "name or area they mention, or empty",
  "start_date": "YYYY-MM-DD or empty",
  "end_date": "YYYY-MM-DD or empty"
}

Intent guide:
- portfolio: how many vacant/not on rent, occupancy summary
- release_date: when guest leaves, when apartment is free (checkout / release)
- block_dates: block bookings on specific dates
- take_offline: take property offline / unavailable for a period (e.g. next month)
- bring_online: put property back on listings
- help: menu, what can you do

Today's date: {today}
Properties: {property_list}
"""


def _parse_llm_json(raw: str) -> dict:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"intent": "unknown", "property_query": "", "start_date": "", "end_date": ""}


def _parse_date(value: str) -> datetime.date | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _fmt_date(d: datetime.date | None) -> str:
    if not d:
        return "—"
    return d.strftime("%d %b %Y")


def _property_list_text(props: list) -> str:
    if not props:
        return "(none yet)"
    return ", ".join(f"{p.title} ({p.area})" for p in props)


def _keyword_intent(text: str) -> dict:
    t = text.lower()
    intent = "unknown"
    if any(w in t for w in ("vacant", "not on rent", "portfolio", "how many", "occupancy", "available units")):
        intent = "portfolio"
    elif any(w in t for w in ("release", "released", "checkout", "check out", "free again", "guest leave")):
        intent = "release_date"
    elif any(w in t for w in ("block", "blackout", "no booking")):
        intent = "block_dates"
    elif any(w in t for w in ("offline", "take off", "unlist", "not available for")):
        intent = "take_offline"
    elif any(w in t for w in ("online", "bring back", "list again", "reactivate")):
        intent = "bring_online"
    elif any(w in t for w in ("help", "menu", "commands")):
        intent = "help"
    return {"intent": intent, "property_query": text, "start_date": "", "end_date": ""}


async def handle_admin_portfolio_message(db: Session, text: str, state: dict) -> tuple[str, dict]:
    """Returns (reply, updated_state)."""
    props = portfolio_properties(db)
    today = datetime.date.today().isoformat()
    prop_names = _property_list_text(props)

    step = state.get("portfolio_step")
    if step == "pick_property":
        prop = match_portfolio_property(db, text)
        if not prop:
            return (
                f"I couldn't match that property. Yours are: {prop_names}. "
                "Reply with the name or area (e.g. Marina).",
                state,
            )
        state["portfolio_property_id"] = prop.id
        pending = state.get("portfolio_pending_intent")
        if pending == "release_date":
            state.pop("portfolio_step", None)
            state.pop("portfolio_pending_intent", None)
            return _reply_release(db, prop), state
        if pending == "bring_online":
            state = {k: v for k, v in state.items() if not k.startswith("portfolio_")}
            bring_property_online(db, prop)
            return f"*{prop.title}* is active again and visible for bookings.", state
        if pending in ("block_dates", "take_offline"):
            state["portfolio_step"] = "ask_start_date"
            return (
                f"Got it — *{prop.title}*. From which date should I "
                f"{'block bookings' if pending == 'block_dates' else 'take it offline'}? (e.g. 2026-08-01)",
                state,
            )

    if step == "ask_start_date":
        start = _parse_date(text)
        if not start:
            return "Please send a start date as YYYY-MM-DD (e.g. 2026-08-01).", state
        state["portfolio_start_date"] = start.isoformat()
        state["portfolio_step"] = "ask_end_date"
        return "And the end date? (YYYY-MM-DD)", state

    if step == "ask_end_date":
        end = _parse_date(text)
        start = _parse_date(state.get("portfolio_start_date", ""))
        if not end or not start:
            return "Please send a valid end date as YYYY-MM-DD.", state
        if end < start:
            return "End date must be on or after the start date. Send the end date again.", state
        prop_id = state.get("portfolio_property_id")
        prop = db.get(Property, prop_id) if prop_id else None
        intent = state.get("portfolio_pending_intent", "block_dates")
        state = {k: v for k, v in state.items() if not k.startswith("portfolio_")}
        if not prop:
            return "Something went wrong — please start again (e.g. 'block Marina from Aug 1 to Aug 10').", state
        try:
            if intent == "take_offline":
                take_property_offline(db, prop, start, end)
                return (
                    f"Done. *{prop.title}* is offline until {_fmt_date(end)} "
                    f"({start} to {end}) and won't accept new bookings.",
                    state,
                )
            block_property_dates(db, prop, start, end)
            return (
                f"Blocked *{prop.title}* from {_fmt_date(start)} to {_fmt_date(end)}. "
                "No guest bookings can be made for those dates.",
                state,
            )
        except ValueError as exc:
            return f"Couldn't do that: {exc}", state

    parsed: dict
    try:
        from app.config import settings as app_settings

        if app_settings.LLM_API_KEY and app_settings.LLM_API_BASE:
            raw = await call_llm(
                _ADMIN_PORTFOLIO_SYSTEM.format(today=today, property_list=prop_names),
                text,
            )
            parsed = _parse_llm_json(raw)
        else:
            parsed = _keyword_intent(text)
    except Exception:
        parsed = _keyword_intent(text)

    intent = parsed.get("intent", "unknown")
    prop_query = parsed.get("property_query", "")
    start_s = parsed.get("start_date", "")
    end_s = parsed.get("end_date", "")

    if intent == "help":
        return _help_text(prop_names), state

    if intent == "portfolio":
        return _reply_portfolio(db), state

    prop = match_portfolio_property(db, prop_query) if prop_query else None

    if intent == "release_date":
        if not prop and len(props) == 1:
            prop = props[0]
        if not prop:
            state["portfolio_step"] = "pick_property"
            state["portfolio_pending_intent"] = "release_date"
            return (
                f"Which property? Reply with the name or area.\nYour units: {prop_names}",
                state,
            )
        return _reply_release(db, prop), state

    if intent in ("block_dates", "take_offline"):
        start = _parse_date(start_s)
        end = _parse_date(end_s)
        if not prop and len(props) == 1:
            prop = props[0]
        if prop and start and end:
            try:
                if intent == "take_offline":
                    take_property_offline(db, prop, start, end)
                    return (
                        f"Done. *{prop.title}* is offline until {_fmt_date(end)} "
                        f"({start} to {end}).",
                        state,
                    )
                block_property_dates(db, prop, start, end)
                return (
                    f"Blocked *{prop.title}* from {_fmt_date(start)} to {_fmt_date(end)}.",
                    state,
                )
            except ValueError as exc:
                return f"Couldn't do that: {exc}", state
        if not prop:
            state["portfolio_step"] = "pick_property"
            state["portfolio_pending_intent"] = intent
            if start:
                state["portfolio_start_date"] = start.isoformat()
            return (
                f"Which property should I "
                f"{'block' if intent == 'block_dates' else 'take offline'}?\n"
                f"Your units: {prop_names}",
                state,
            )
        state["portfolio_property_id"] = prop.id
        state["portfolio_pending_intent"] = intent
        if not start:
            state["portfolio_step"] = "ask_start_date"
            return f"From which date for *{prop.title}*? (YYYY-MM-DD)", state
        if not end:
            state["portfolio_start_date"] = start.isoformat()
            state["portfolio_step"] = "ask_end_date"
            return f"Until which date for *{prop.title}*? (YYYY-MM-DD)", state

    if intent == "bring_online":
        if not prop and len(props) == 1:
            prop = props[0]
        if not prop:
            state["portfolio_step"] = "pick_property"
            state["portfolio_pending_intent"] = "bring_online"
            return f"Which property should go back online?\nYour units: {prop_names}", state
        bring_property_online(db, prop)
        return f"*{prop.title}* is active again and visible for bookings.", state

    return (
        "Portfolio commands (admin):\n"
        "• How many apartments are vacant?\n"
        "• When is my Marina apartment released by the guest?\n"
        "• Block JBR Penthouse from 2026-08-01 to 2026-08-15\n"
        "• Take Downtown Studio offline for next month\n\n"
        f"Your properties: {prop_names}",
        state,
    )


def _help_text(prop_names: str) -> str:
    return (
        "*Admin portfolio commands (UAE Stays)*\n\n"
        "• *Portfolio* — how many units are vacant / on rent\n"
        "• *Release date* — when a guest checks out / unit is free\n"
        "• *Block dates* — stop bookings on specific dates\n"
        "• *Take offline* — hide a unit for a period\n"
        "• *Bring online* — list a unit again\n\n"
        f"Properties: {prop_names}"
    )


def _reply_portfolio(db: Session) -> str:
    summary = portfolio_summary(db)
    lines = [
        f"*Your portfolio* (as of {_fmt_date(summary['as_of'])})",
        f"Total: {summary['total']} | Vacant: {len(summary['vacant'])} | "
        f"On rent/blocked: {len(summary['occupied'])} | Offline: {len(summary['offline'])}",
    ]
    if summary["vacant"]:
        lines.append("\n*Vacant now:*")
        for p in summary["vacant"]:
            lines.append(f"• {p.title} ({p.area})")
    if summary["occupied"]:
        lines.append("\n*Not available (booked/blocked):*")
        for p in summary["occupied"]:
            info = next_release_info(db, p)
            rel = info.get("available_from")
            extra = f" — free from {_fmt_date(rel)}" if rel else ""
            lines.append(f"• {p.title}{extra}")
    if summary["offline"]:
        lines.append("\n*Offline:*")
        for p in summary["offline"]:
            lines.append(f"• {p.title}")
    return "\n".join(lines)


def _reply_release(db: Session, prop) -> str:
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
