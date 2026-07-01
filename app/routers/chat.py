from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.chatbot.graph import chatbot_graph
from app.models.models import ChatbotSession, ChatbotMessage
from pydantic import BaseModel

router = APIRouter(prefix="/chat", tags=["chat"])

class ChatRequest(BaseModel):
    session_id: str
    message: str

@router.post("/message")
async def send_message(req: ChatRequest, db: Session = Depends(get_db)):
    session = db.query(ChatbotSession).filter(ChatbotSession.id == req.session_id).first()
    if not session:
        session = ChatbotSession(id=req.session_id, phone="web", state={})
        db.add(session)
        db.flush()

    db.add(ChatbotMessage(session_id=session.id, direction="inbound", message_text=req.message))

    state = dict(session.state or {})
    state["session_id"] = session.id
    state["phone"] = "web"
    state["incoming_message"] = req.message

    result_state = await chatbot_graph.ainvoke(state)
    reply = result_state.get("reply", "Sorry, I didn't quite get that.")

    # persist only the resumable fields, drop transient ones
    session.state = {k: v for k, v in result_state.items() if k not in ("incoming_message", "reply")}
    session.last_intent = result_state.get("intent")
    db.add(ChatbotMessage(session_id=session.id, direction="outbound", message_text=reply))
    db.commit()

    return {"reply": reply}

@router.get("/history/{session_id}")
def get_history(session_id: str, db: Session = Depends(get_db)):
    messages = db.query(ChatbotMessage).filter(ChatbotMessage.session_id == session_id).order_by(ChatbotMessage.created_at.asc()).all()
    return [{"direction": m.direction, "text": m.message_text, "created_at": m.created_at} for m in messages]
