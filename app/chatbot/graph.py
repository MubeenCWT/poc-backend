import httpx
from langgraph.graph import StateGraph, END
import json

from app.chatbot.state import ChatState
from app.chatbot.llm_client import call_llm

API_BASE = "http://localhost:8000"

async def classify_intent(state: ChatState) -> ChatState:
    if state.get("intent"):
        return state

    system_prompt = (
        "Classify the user's message into exactly one of: "
        "booking, maintenance, discount_check, general. "
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

async def handle_booking(state: ChatState) -> ChatState:
    step = state.get("current_step", "ask_name")
    msg = state["incoming_message"]

    if step == "ask_name":
        state["reply"] = "May I have your full name please?"
        state["current_step"] = "wait_name"
        return state

    if step == "wait_name":
        prompt = "Extract the person's full name from this message. Return ONLY the name."
        name = await call_llm(prompt, msg)
        state["guest_name"] = name.strip()
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{API_BASE}/api/properties/")
            props = resp.json() if resp.status_code == 200 else []
            
        prop_list = "\n".join([f"- {p['title']} (AED {p['price_daily']}/day)" for p in props])
        state["reply"] = f"Which property would you like, {state['guest_name']}?\n\nAvailable properties:\n{prop_list}"
        state["current_step"] = "wait_property"
        return state

    if step == "wait_property":
        prompt = "Extract the property name or area the user is referring to. Return ONLY the name/area."
        prop_query = await call_llm(prompt, msg)
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{API_BASE}/api/properties/")
            props = resp.json() if resp.status_code == 200 else []
            
        matched = next((p for p in props if prop_query.lower() in p["title"].lower()), None)
        
        if not matched:
            state["reply"] = "I couldn't find that property. Which one would you like?"
            return state
            
        state["selected_property"] = matched["id"]
        state["reply"] = f"Great choice! What is your preferred check-in date?"
        state["current_step"] = "wait_checkin"
        return state

    import datetime
    current_year = datetime.datetime.now().year

    if step == "wait_checkin":
        prompt = f"Extract the check-in date from this message and format it as YYYY-MM-DD. Assume the year is {current_year} unless specified otherwise. Return ONLY the date."
        date = await call_llm(prompt, msg)
        state["start_date"] = date.strip()
        state["reply"] = "And your check-out date?"
        state["current_step"] = "wait_checkout"
        return state

    if step == "wait_checkout":
        prompt = f"Extract the check-out date from this message and format it as YYYY-MM-DD. Assume the year is {current_year} unless specified otherwise. Return ONLY the date."
        date = await call_llm(prompt, msg)
        state["end_date"] = date.strip()
        state["reply"] = "Would you like daily, monthly, or yearly rental?"
        state["current_step"] = "wait_type"
        return state

    if step == "wait_type":
        if "daily" in msg.lower():
            state["booking_type"] = "daily"
        elif "monthly" in msg.lower():
            state["booking_type"] = "monthly"
        elif "yearly" in msg.lower():
            state["booking_type"] = "yearly"
        else:
            state["reply"] = "Please specify daily, monthly, or yearly."
            return state

        # Check availability & get quote
        payload = {
            "property_id": state["selected_property"],
            "guest_name": state["guest_name"],
            "guest_phone": "web",
            "booking_type": state["booking_type"],
            "start_date": state["start_date"],
            "end_date": state["end_date"],
            "discount_requested": False,
            "source": "chatbot"
        }
        
        async with httpx.AsyncClient() as client:
            # Check availability first
            avail_resp = await client.get(
                f"{API_BASE}/api/bookings/check-availability",
                params={
                    "property_id": state["selected_property"],
                    "start_date": state["start_date"],
                    "end_date": state["end_date"]
                }
            )
            avail_data = avail_resp.json()
            
            if not avail_data.get("available", True):
                state["reply"] = "Sorry, this property is already booked for those dates. Let's try different dates. When would you like to check in?"
                state["current_step"] = "wait_checkin"
                return state

            prop_resp = await client.get(f"{API_BASE}/api/properties/{state['selected_property']}")
            prop = prop_resp.json()
            
            price_map = {"daily": prop["price_daily"], "monthly": prop["price_monthly"], "yearly": prop["price_yearly"]}
            state["quote_amount"] = price_map[state["booking_type"]]
            
            state["reply"] = (
                f"Here is your quote:\n"
                f"Property: *{prop['title']}*\n"
                f"Dates: {state['start_date']} to {state['end_date']}\n"
                f"Type: *{state['booking_type']}*\n\n"
                f"Shall I confirm this booking?"
            )
            state["current_step"] = "wait_confirm"
        return state

    if step == "wait_confirm":
        if "yes" in msg.lower() or "confirm" in msg.lower() or "ok" in msg.lower():
            payload = {
                "property_id": state["selected_property"],
                "guest_name": state["guest_name"],
                "guest_phone": "web",
                "booking_type": state["booking_type"],
                "start_date": state["start_date"],
                "end_date": state["end_date"],
                "source": "chatbot"
            }
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{API_BASE}/api/bookings/", json=payload)
                
            if resp.status_code != 200:
                error_msg = resp.json().get("detail", "the property is not available for those dates.") if resp.status_code == 400 else "the property is not available for those dates."
                state["reply"] = f"Sorry, {error_msg} Let's try again."
                state["intent"] = None
                return state
                
            booking = resp.json()
            state["reply"] = (
                f"✅ Booking confirmed!\n"
                f"Reference: *{booking['id'][:8].upper()}*\n"
                f"Dates: {booking['start_date']} to {booking['end_date']}\n"
                f"Total: *AED {booking['base_price']}*\n\n"
                f"Please transfer the amount and send your receipt here to complete."
            )
            state["intent"] = None
        else:
            state["reply"] = "Booking cancelled. How else can I help you?"
            state["intent"] = None
            
        return state

    return state

async def handle_maintenance(state: ChatState) -> ChatState:
    step = state.get("current_step", "ask_unit")
    msg = state["incoming_message"]

    if step == "ask_unit":
        state["reply"] = "Which unit/property are you in?"
        state["current_step"] = "wait_unit"
        return state
        
    if step == "wait_unit":
        prompt = "Extract the property name or area the user is referring to. Return ONLY the name/area."
        prop_query = await call_llm(prompt, msg)
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{API_BASE}/api/properties/")
            props = resp.json() if resp.status_code == 200 else []
            
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
        if "plumb" in type_str: state["issue_type"] = "plumbing"
        elif "elect" in type_str: state["issue_type"] = "electrical"
        elif "ac" in type_str: state["issue_type"] = "AC"
        elif "clean" in type_str: state["issue_type"] = "cleaning"
        else: state["issue_type"] = "other"
        
        state["reply"] = "Could you please describe the issue in a little more detail?"
        state["current_step"] = "wait_desc"
        return state

    if step == "wait_desc":
        state["issue_description"] = msg
        
        payload = {
            "property_id": state["unit_id"],
            "requested_by": "Web Guest",
            "issue_type": state["issue_type"],
            "description": state["issue_description"]
        }
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{API_BASE}/api/maintenance/", json=payload)
            data = resp.json()
            
        state["reply"] = (
            f"✅ Your {state['issue_type']} request has been logged (Ref: *{data['request_id'][:8].upper()}*).\n"
            f"Our team has been notified and will arrange this for you shortly."
        )
        state["intent"] = None
        return state
        
    return state

async def handle_discount_check(state: ChatState) -> ChatState:
    state["reply"] = "Discount requests are handled by our admin team. I've noted your interest — someone will get back to you shortly."
    state["intent"] = None
    return state

async def handle_general(state: ChatState) -> ChatState:
    system_prompt = (
        "You are a friendly web assistant for a UAE property booking platform. "
        "Answer briefly and helpfully. If relevant, invite the user to book a property, "
        "or report a maintenance issue."
    )
    reply = await call_llm(system_prompt, state["incoming_message"])
    state["reply"] = reply
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
