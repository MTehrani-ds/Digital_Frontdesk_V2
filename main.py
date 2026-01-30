from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# Models: Policy + State machine
# ============================================================

class Step(str, Enum):
    LIMITED_RESPONSE = "LIMITED_RESPONSE"   # safety gate / safe response
    COLLECT_CONTACT = "COLLECT_CONTACT"     # collect callback details
    HANDOFF = "HANDOFF"                     # handed off to staff (ticket created)


class Collected(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    best_time: Optional[str] = None


class SessionState(BaseModel):
    step: Step = Step.LIMITED_RESPONSE
    procedure: Optional[str] = None
    intent: Optional[str] = None  # context carryover (e.g., pricing -> service selection)
    collected: Collected = Field(default_factory=Collected)

    # NEW: store what the patient asked (for staff ticket context)
    topic: Optional[str] = None
    initial_question: Optional[str] = None
    last_question: Optional[str] = None


class IncomingMessage(BaseModel):
    # Keep API shape stable
    session_id: str
    user_message: str
    channel: str = "webchat"
    practice_name: str = "Example Dental Clinic"
    prior_state: Optional[SessionState] = None
    msg: Optional[str] = None
    state: Optional[SessionState] = None


class OutgoingMessage(BaseModel):
    session_id: str
    channel: str
    practice_name: str
    user_message: str
    reply: str
    state: SessionState
    ticket: Optional[Dict[str, Any]] = None


# ============================================================
# Ticketing (Path A): in-memory callback tasks
# ============================================================

class Ticket(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticket_id: str
    created_at: str
    session_id: str
    practice_name: str
    name: str
    phone: str
    best_time: str
    summary: str
    status: str = "OPEN"
    ticket_type: str = "CALLBACK"  # APPOINTMENT / CANCEL / EMERGENCY / CALLBACK


TICKET_DB: List[Ticket] = []
TICKET_COUNTER = 0


def create_ticket(
    session_id: str,
    practice_name: str,
    state: SessionState,
    summary: str,
    ticket_type: str = "CALLBACK",
) -> Ticket:
    global TICKET_COUNTER
    TICKET_COUNTER += 1
    ticket = Ticket(
        ticket_id=f"T-{TICKET_COUNTER:04d}",
        created_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        session_id=session_id,
        practice_name=practice_name,
        name=state.collected.name or "",
        phone=state.collected.phone or "",
        best_time=state.collected.best_time or "",
        summary=summary,
        status="OPEN",
        ticket_type=ticket_type,
    )
    TICKET_DB.insert(0, ticket)  # newest first
    return ticket


# ============================================================
# App + error handler
# ============================================================

app = FastAPI(title="Digital Frontdesk – Dental Clinic Demo", version="1.1.0")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.exception("Unhandled server error")
    return JSONResponse(status_code=500, content={"error": "internal_error", "detail": str(exc)})


# ============================================================
# In-memory session store
# ============================================================

SESSION_DB: Dict[str, SessionState] = {}


def get_state(payload: IncomingMessage) -> SessionState:
    if payload.state is not None:
        return payload.state
    if payload.prior_state is not None:
        return payload.prior_state
    if payload.session_id in SESSION_DB:
        return SESSION_DB[payload.session_id]
    return SessionState()


def save_state(session_id: str, state: SessionState) -> None:
    SESSION_DB[session_id] = state


# ============================================================
# Clinic configuration (demo placeholders)
# ============================================================

CLINIC = {
    "name": "Example Dental Clinic",
    "address": "Example Street 12, 1010 Vienna",
    "phone": "+43 1 234 5678",
    "hours": "Mon–Fri 08:00–18:00",
    "parking": "Street parking nearby. Nearest garage: Example Garage (3 min walk).",
    "public_transport": "U1/U3 to Stephansplatz, then 5 min walk (demo text).",
    "cancellation_policy": "Please cancel or reschedule at least 24 hours in advance.",
    "insurance": "We accept public insurance and private pay (demo).",
    "what_to_bring": "E-card/insurance card, photo ID, medication list (if any), and prior dental records if available.",
    "emergency_note": (
        "If you have severe swelling, fever, heavy bleeding, trouble swallowing/breathing, "
        "or rapidly worsening pain, seek urgent care immediately."
    ),
    "services": [
        "Check-ups & consultations",
        "Professional cleaning",
        "Fillings",
        "Root canal treatment (by assessment)",
        "Crowns/bridges",
        "Implants (by assessment)",
        "Kids dentistry",
        "Emergency pain consultations",
    ],
}


# ============================================================
# Policy layer (industry-safe)
# ============================================================

def is_medical_or_medication_question(text: str) -> bool:
    t = text.lower()
    keywords = [
        "antibiotic", "antibiotics", "amoxicillin", "penicillin", "clindamycin",
        "medicine", "medication", "dose", "dosage", "should i",
        "ibuprofen", "painkiller", "prescription",
        "diagnose", "abscess", "infection",
    ]
    return any(k in t for k in keywords)


def limited_response_policy(practice_name: str) -> str:
    return (
        "Thanks for your message. I can’t recommend specific medication or diagnose a condition "
        "without a clinician evaluating your situation.\n\n"
        f"{CLINIC['emergency_note']}\n\n"
        f"If not urgent: the safest next step is to speak with a dentist from {practice_name}. "
        "Please share your **name**, **phone number**, and the **best time** to call you back."
    )


# ============================================================
# FAQ (starter set) + router
# ============================================================

FAQ: List[Dict[str, Any]] = [
    {
        "key": "hours",
        "priority": 20,
        "keywords": ["hours", "opening", "open", "close", "closing", "weekend", "saturday", "sunday"],
        "answer": lambda: f"Our opening hours are: {CLINIC['hours']}.",
    },
    {
        "key": "location_parking",
        "priority": 20,
        "keywords": ["address", "location", "where", "parking", "park", "garage", "public transport", "tram", "metro", "u-bahn", "bus"],
        "answer": lambda: (
            f"Address: {CLINIC['address']}.\n"
            f"Parking: {CLINIC['parking']}\n"
            f"Public transport: {CLINIC['public_transport']}"
        ),
    },
    {
        "key": "booking",
        "priority": 25,
        "keywords": ["appointment", "book", "booking", "schedule", "availability", "available"],
        "answer": lambda: (
            "I can help arrange an appointment request. "
            "Please share your **name**, **phone number**, and your **best time window** to reach you."
        ),
        "forces_contact_flow": True,
        "ticket_type": "APPOINTMENT",
    },
    {
        "key": "reschedule_cancel",
        "priority": 25,
        "keywords": ["reschedule", "change appointment", "move appointment", "cancel", "cancellation"],
        "answer": lambda: (
            "To cancel or reschedule, please share your **name**, **phone number**, and your preferred new time window.\n\n"
            f"Policy: {CLINIC['cancellation_policy']}"
        ),
        "forces_contact_flow": True,
        "ticket_type": "CANCEL",
    },
    {
        "key": "emergency",
        "priority": 30,
        "keywords": ["emergency", "urgent", "swelling", "bleeding", "fever", "can't breathe", "can’t breathe", "hard to swallow", "severe pain"],
        "answer": lambda: (
            f"{CLINIC['emergency_note']}\n\n"
            "If you want a same-day assessment, share your **name**, **phone number**, and **best time** to call."
        ),
        "forces_contact_flow": True,
        "ticket_type": "EMERGENCY",
    },
    {
        "key": "what_to_bring",
        "priority": 20,
        "keywords": ["what to bring", "bring", "documents", "paperwork", "e-card", "id", "records", "first visit", "new patient"],
        "answer": lambda: f"For your visit, please bring: {CLINIC['what_to_bring']}",
    },
    {
        "key": "pricing_insurance",
        "priority": 35,
        "keywords": ["price", "pricing", "cost", "how much", "insurance", "kassa", "private", "payment", "accept insurance"],
        "answer": lambda: (
            f"Pricing depends on the service and insurance coverage. {CLINIC['insurance']}\n"
            "Which service is this about (e.g., cleaning, filling, implant)?"
        ),
        "sets_intent": "pricing_insurance",
    },
    {
        "key": "services",
        "priority": 10,
        "keywords": ["services", "what do you offer", "do you do", "offer", "treatments", "treatment options"],
        "answer": lambda: (
            "We offer:\n- " + "\n- ".join(CLINIC["services"]) +
            "\n\nIf you'd like, tell me what you need and I can arrange a callback."
        ),
    },
]


def match_faq_intent(text: str) -> Optional[Dict[str, Any]]:
    t = text.lower()
    hits: List[Dict[str, Any]] = []
    for item in FAQ:
        if any(k in t for k in item["keywords"]):
            hits.append(item)
    if not hits:
        return None
    hits.sort(key=lambda x: x.get("priority", 0), reverse=True)
    return hits[0]


def normalize_service(text: str) -> Optional[str]:
    t = text.lower().strip()
    mapping = {
        "implant": ["implant", "implants"],
        "cleaning": ["cleaning", "hygiene", "professional cleaning"],
        "filling": ["filling", "fillings"],
        "checkup": ["checkup", "check-up", "check ups", "consultation", "consult"],
        "root canal": ["root canal", "endo", "endodontic"],
        "kids dentistry": ["kids", "child", "children", "pediatric"],
        "crown/bridge": ["crown", "bridge", "crowns", "bridges"],
    }
    for service, keys in mapping.items():
        if any(k in t for k in keys):
            return service
    if t in ["implant", "implants", "cleaning", "filling", "checkup", "check-up"]:
        return t.replace("check-up", "checkup").rstrip("s")
    return None


# ============================================================
# Ticket context helpers
# ============================================================

def looks_like_contact_message(text: str) -> bool:
    t = text.lower()
    has_labels = any(x in t for x in ["name:", "phone:", "tel:", "mobile:", "best time"])
    digits = sum(ch.isdigit() for ch in text)
    return has_labels or digits >= 7


def set_topic_from_faq_key(state: SessionState, key: str) -> None:
    mapping = {
        "pricing_insurance": "Pricing / insurance",
        "booking": "Appointment request",
        "reschedule_cancel": "Cancel / reschedule",
        "emergency": "Urgent dental issue",
        "services": "Services inquiry",
        "hours": "Opening hours",
        "location_parking": "Location / parking",
        "what_to_bring": "First visit / documents",
    }
    state.topic = mapping.get(key, state.topic or "General inquiry")


# ============================================================
# Extraction (robust for Name/Phone/Best time)
# ============================================================

def update_collected_from_text(state: SessionState, user_text: str) -> SessionState:
    if state.collected is None:
        state.collected = Collected()

    text = user_text.strip()
    tl = text.lower()

    # PHONE
    if not state.collected.phone:
        for key in ["phone:", "phone=", "phone-", "tel:", "tel=", "tel-", "mobile:", "mobile=", "mobile-"]:
            pos = tl.find(key)
            if pos != -1:
                candidate = text[pos + len(key):].strip()
                lowcand = candidate.lower()
                for stop in [" best time", " name", ",", ";", "."]:
                    idx2 = lowcand.find(stop)
                    if idx2 != -1:
                        candidate = candidate[:idx2].strip()
                        lowcand = candidate.lower()
                digits = "".join(ch for ch in candidate if ch.isdigit() or ch == "+")
                if len(digits.replace("+", "")) >= 9:
                    state.collected.phone = digits
                break

    if not state.collected.phone:
        digits = "".join(ch for ch in text if ch.isdigit() or ch == "+")
        if len(digits.replace("+", "")) >= 9:
            state.collected.phone = digits

    # BEST TIME
    if not state.collected.best_time:
        marker = "best time"
        if marker in tl:
            start = tl.find(marker)
            best = text[start:].strip()
            best_low = best.lower()
            if best_low.startswith("best time"):
                best = best[len("best time"):].lstrip(" :,-=").strip()

            lowbest = best.lower()
            for stop in [" phone", " name", ".", ";", "|"]:
                idx2 = lowbest.find(stop)
                if idx2 != -1:
                    best = best[:idx2].strip()
                    lowbest = best.lower()

            if best:
                state.collected.best_time = best
        else:
            for phrase in [
                "tomorrow morning", "tomorrow afternoon", "tomorrow evening",
                "today morning", "today afternoon", "today evening",
                "tomorrow", "today", "anytime"
            ]:
                if phrase in tl:
                    state.collected.best_time = phrase
                    break

    # NAME
    if not state.collected.name:
        for sep in [":", "=", "-"]:
            key = "name" + sep
            pos = tl.find(key)
            if pos != -1:
                candidate = text[pos + len(key):].strip()
                lowcand = candidate.lower()
                for stop in [" phone", " best time", ",", ";", "."]:
                    idx2 = lowcand.find(stop)
                    if idx2 != -1:
                        candidate = candidate[:idx2].strip()
                        lowcand = candidate.lower()
                if len(candidate) >= 2:
                    state.collected.name = candidate[:80]
                break

    if not state.collected.name:
        for pat in ["my name is ", "i am ", "i'm "]:
            idx = tl.find(pat)
            if idx != -1:
                name = text[idx + len(pat):].strip()
                lowname = name.lower()
                for stop in [" phone", " best time", ".", ",", ";", " and "]:
                    idx2 = lowname.find(stop)
                    if idx2 != -1:
                        name = name[:idx2].strip()
                        lowname = name.lower()
                if len(name) >= 2:
                    state.collected.name = name[:80]
                break

    return state


# ============================================================
# State machine: policy + FAQ + context carryover + contact flow
# ============================================================

def next_reply(practice_name: str, user_text: str, state: SessionState) -> tuple[str, SessionState]:
    # Remember question context (ignore contact-only messages)
    if not looks_like_contact_message(user_text):
        state.last_question = user_text.strip()[:240]
        if not state.initial_question:
            state.initial_question = state.last_question

    # 1) Policy gate (medical/medication)
    if is_medical_or_medication_question(user_text):
        state.step = Step.LIMITED_RESPONSE
        state.intent = None
        set_topic_from_faq_key(state, "emergency")  # closest "staff-relevant" topic
        return limited_response_policy(practice_name), state

    # 2) Context carryover: pricing -> service selection
    if state.intent == "pricing_insurance":
        service = normalize_service(user_text)
        if service:
            state.intent = None
            state.step = Step.COLLECT_CONTACT
            state.topic = "Pricing / insurance"
            # store the service in last_question context too (useful for summary)
            state.last_question = f"Service: {service}"
            return (
                f"Got it — **{service}**.\n\n"
                f"Coverage and costs depend on insurance status and clinical assessment. {CLINIC['insurance']}\n\n"
                "If you want, I can arrange a callback to provide an estimated range. "
                "Please share your **name**, **phone number**, and **best time**.",
                state,
            )
        return "Which service is this about (e.g., cleaning, filling, implant)?", state

    # 3) FAQ router (safe front-desk topics)
    faq = match_faq_intent(user_text)
    if faq:
        answer = faq["answer"]()
        set_topic_from_faq_key(state, faq["key"])

        if faq.get("sets_intent"):
            state.intent = faq["sets_intent"]

        forces_contact = bool(faq.get("forces_contact_flow"))
        if forces_contact:
            state.step = Step.COLLECT_CONTACT
            state = update_collected_from_text(state, user_text)

            missing = []
            if not state.collected.name:
                missing.append("name")
            if not state.collected.phone:
                missing.append("phone number")
            if not state.collected.best_time:
                missing.append("best time to call")

            if missing:
                answer += "\n\nTo proceed, I still need your " + ", ".join(missing) + "."
                return answer, state

            state.step = Step.HANDOFF
            return (
                f"{answer}\n\n"
                f"Thanks, {state.collected.name}. I’ve captured your details.\n\n"
                f"Phone: {state.collected.phone}\n"
                f"Best time: {state.collected.best_time}\n\n"
                f"Someone from {practice_name} will contact you shortly.",
                state,
            )

        # Simple FAQ response
        if state.step != Step.COLLECT_CONTACT:
            return answer, state

        # If collecting contact, remind missing fields
        missing = []
        if not state.collected.name:
            missing.append("name")
        if not state.collected.phone:
            missing.append("phone number")
        if not state.collected.best_time:
            missing.append("best time to call")
        if missing:
            answer += "\n\nTo proceed, I still need your " + ", ".join(missing) + "."
        return answer, state

    # 4) Contact collection flow (fallback)
    if state.step in (Step.LIMITED_RESPONSE, Step.COLLECT_CONTACT):
        state = update_collected_from_text(state, user_text)

        missing = []
        if not state.collected.name:
            missing.append("name")
        if not state.collected.phone:
            missing.append("phone number")
        if not state.collected.best_time:
            missing.append("best time to call")

        if missing:
            state.step = Step.COLLECT_CONTACT
            return (
                "To arrange a callback, I still need your " + ", ".join(missing) +
                ". You can reply in one message like: “Name: …, Phone: …, Best time: …”.",
                state,
            )

        state.step = Step.HANDOFF
        return (
            f"Thanks, {state.collected.name}. I’ve captured your details.\n\n"
            f"Phone: {state.collected.phone}\n"
            f"Best time: {state.collected.best_time}\n\n"
            f"Someone from {practice_name} will contact you shortly.",
            state,
        )

    # 5) Already handed off
    return (f"Thanks — your request is with the team at {practice_name}.", state)


# ============================================================
# Routes
# ============================================================

@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/webchat/message", response_model=OutgoingMessage)
def webchat_message(payload: IncomingMessage) -> OutgoingMessage:
    old_state = get_state(payload)
    old_step = old_state.step  # snapshot BEFORE mutation in next_reply

    reply, new_state = next_reply(payload.practice_name, payload.user_message, old_state)
    save_state(payload.session_id, new_state)

    ticket_info: Optional[Dict[str, Any]] = None

    # Create ticket ONLY on transition into HANDOFF
    if old_step != Step.HANDOFF and new_state.step == Step.HANDOFF:
        # Derive ticket type from topic (preferred) or keywords
        ticket_type = "CALLBACK"
        if (new_state.topic or "").lower().startswith("appointment"):
            ticket_type = "APPOINTMENT"
        elif "cancel" in (new_state.topic or "").lower():
            ticket_type = "CANCEL"
        elif "urgent" in (new_state.topic or "").lower():
            ticket_type = "EMERGENCY"

        # Build a staff-friendly summary using remembered context
        topic = new_state.topic or "General inquiry"
        initial_q = new_state.initial_question or ""
        last_q = new_state.last_question or ""

        parts = [f"Topic: {topic}"]
        if initial_q:
            parts.append(f"Initial: {initial_q}")
        if last_q and last_q != initial_q:
            parts.append(f"Latest: {last_q}")

        summary = " | ".join(parts)

        ticket = create_ticket(
            payload.session_id,
            payload.practice_name,
            new_state,
            summary=summary,
            ticket_type=ticket_type,
        )
        ticket_info = {
            "ticket_id": ticket.ticket_id,
            "status": ticket.status,
            "created_at": ticket.created_at,
            "ticket_type": ticket.ticket_type,
        }
        reply = reply + f"\n\n✅ Callback ticket created: {ticket.ticket_id}"

    return OutgoingMessage(
        session_id=payload.session_id,
        channel=payload.channel,
        practice_name=payload.practice_name,
        user_message=payload.user_message,
        reply=reply,
        state=new_state,
        ticket=ticket_info,
    )


@app.post("/admin/reset_session/{session_id}")
def reset_session(session_id: str) -> Dict[str, Any]:
    SESSION_DB.pop(session_id, None)
    global TICKET_DB
    TICKET_DB = [t for t in TICKET_DB if t.session_id != session_id]
    return {"ok": True, "session_id": session_id}


@app.get("/staff", response_class=HTMLResponse)
def staff_dashboard():
    rows = []
    for t in TICKET_DB[:50]:
        rows.append(f"""
        <tr>
          <td>{t.ticket_id}</td>
          <td>{t.ticket_type}</td>
          <td>{t.created_at}</td>
          <td>{t.name}</td>
          <td>{t.phone}</td>
          <td>{t.best_time}</td>
          <td>{t.summary}</td>
          <td>{t.status}</td>
        </tr>
        """)

    html = f"""
    <html>
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width,initial-scale=1" />
      <title>Staff Dashboard</title>
      <style>
        body {{ font-family: Arial, sans-serif; background:#f5f5f5; margin:0; padding:24px; }}
        .card {{ background:#fff; border-radius:14px; padding:16px; box-shadow: 0 2px 10px rgba(0,0,0,.08); }}
        table {{ width:100%; border-collapse: collapse; }}
        th, td {{ border-bottom: 1px solid #eee; text-align:left; padding:10px; font-size:14px; vertical-align: top; }}
        th {{ background:#fafafa; }}
        .top {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; gap:12px; flex-wrap: wrap; }}
        a.button {{ padding:8px 12px; border:1px solid #ddd; border-radius:10px; text-decoration:none; color:#333; background:#fff; }}
        .hint {{ color:#666; font-size:12px; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="top">
          <div>
            <h3 style="margin:0;">Dental Clinic — Callback Tasks</h3>
            <div class="hint">Demo inbox (in-memory). Refresh to see new tickets.</div>
          </div>
          <div>
            <a class="button" href="/">Open Chat</a>
            <a class="button" href="/staff">Refresh</a>
            <a class="button" href="/docs">API Docs</a>
          </div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Ticket</th>
              <th>Type</th>
              <th>Created (UTC)</th>
              <th>Name</th>
              <th>Phone</th>
              <th>Best time</th>
              <th>Reason / Summary</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {''.join(rows) if rows else '<tr><td colspan="8">No tickets yet.</td></tr>'}
          </tbody>
        </table>
      </div>
    </body>
    </html>
    """
    return html


@app.get("/", response_class=HTMLResponse)
def home():
    try:
        with open("chat.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return (
            "<h3>chat.html not found</h3>"
            "<p>Create <b>chat.html</b> next to <b>main.py</b> to use the chat UI.</p>"
            "<p>You can still test the API via <a href='/docs'>/docs</a>.</p>"
        )
