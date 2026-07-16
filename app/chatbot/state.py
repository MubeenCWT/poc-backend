from typing import TypedDict, Optional, List, Dict, Any

class ChatState(TypedDict, total=False):
    session_id: str
    phone: str
    incoming_message: str
    intent: Optional[str]  # booking, maintenance, discount_check, availability, general
    reply: Optional[str]

    # multi-step flow
    current_step: Optional[str]

    # booking slot-filling
    guest_name: Optional[str]
    selected_property: Optional[str]
    start_date: Optional[str]
    end_date: Optional[str]
    booking_type: Optional[str]
    months_count: Optional[int]
    wants_discount: Optional[bool]
    quote_amount: Optional[float]
    booking_id: Optional[str]

    # maintenance slot-filling
    unit_id: Optional[str]
    issue_type: Optional[str]
    issue_description: Optional[str]

    # discount status check
    check_name: Optional[str]

    # interactive reply buttons for WhatsApp (optional)
    reply_buttons: Optional[List[Dict[str, str]]]

    # counter-offer amount awaiting tenant response
    counter_amount: Optional[float]
