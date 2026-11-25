"""
main.py – FastAPI application entrypoint.
Serves the static frontend and all API routes.
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timedelta, date, time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from models import (
    Event, Proposal, SlotOption, ChatMessage, ApplyRequest, RejectRequest, BackendRequest,
    EventType,
)
from calendar_store import MockCalendarStore, GoogleCalendarStore
from agent import KairosAgent
from scheduling import health_score, detect_conflicts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Kairos – AI Calendar", version="1.0.0")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Backend selection (global mutable state; fine for single-process demo)
# ---------------------------------------------------------------------------

def _choose_backend() -> str:
    env_backend = os.getenv("CALENDAR_BACKEND", "")
    if env_backend in ("mock", "google"):
        return env_backend
    # Auto-detect: use google if token.json exists
    if Path("token.json").exists():
        return "google"
    return "mock"


_backend_name: str = _choose_backend()
_mock_store = MockCalendarStore()
_google_store = GoogleCalendarStore(calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary"))


def get_store():
    if _backend_name == "google" and _google_store.is_authenticated():
        return _google_store
    return _mock_store


def get_agent() -> KairosAgent:
    return KairosAgent(get_store())


# Keep a single agent instance per request (stateless for now)
# For multi-turn, we'd use session keys – keeping simple for prototype
_agent = KairosAgent(get_store())


def _refresh_agent():
    global _agent
    _agent = KairosAgent(get_store())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _week_range():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    start = datetime.combine(monday, time(0, 0))
    end = start + timedelta(days=7)
    return start, end


def _serialize_event(e: Event) -> dict:
    return {
        "id": e.id,
        "title": e.title,
        "start": e.start.isoformat(),
        "end": e.end.isoformat(),
        "attendees": [{"email": a.email, "name": a.name} for a in e.attendees],
        "type": e.type,
        "location": e.location,
        "description": e.description,
        "duration_minutes": e.duration_minutes,
    }


def _serialize_proposal(p: Optional[Proposal]) -> Optional[dict]:
    if not p:
        return None
    return {
        "id": p.id,
        "summary": p.summary,
        "reasoning": p.reasoning,
        "changes": [
            {
                "action": c.action,
                "event": _serialize_event(c.event),
                "before": _serialize_event(c.before) if c.before else None,
            }
            for c in p.changes
        ],
    }


def _serialize_slot(s: SlotOption) -> dict:
    return {
        "start": s.start.isoformat(),
        "end": s.end.isoformat(),
        "score": s.score,
        "why": s.why,
    }


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(str(index_path))


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/events")
async def api_events():
    start, end = _week_range()
    events = get_store().list_events(start, end)
    conflicts = detect_conflicts(events)
    conflict_ids = set()
    for e1, e2 in conflicts:
        conflict_ids.add(e1.id)
        conflict_ids.add(e2.id)

    serialized = []
    for e in events:
        d = _serialize_event(e)
        d["has_conflict"] = e.id in conflict_ids
        serialized.append(d)

    return {
        "events": serialized,
        "backend": _backend_name,
        "google_authed": _google_store.is_authenticated(),
    }


@app.get("/api/brief")
async def api_brief():
    start, end = _week_range()
    events = get_store().list_events(start, end)
    report = health_score(events)
    # Try to generate an AI summary; fall back to template
    ai_summary = None
    if _agent._gemini_available:
        try:
            result = _agent._tool_get_calendar_health()
            # Build a quick summary
            score = result["score"]
            meeting_h = result["meeting_hours"]
            focus_h = result["focus_hours"]
            ai_summary = (
                f"Your week scores {score}/100. You have {meeting_h}h of meetings "
                f"and {focus_h}h of focus time scheduled."
            )
        except Exception:
            pass

    if not ai_summary:
        score = report.score
        ai_summary = (
            f"Your week scores {score}/100. "
            f"{report.meeting_hours}h in meetings, {report.focus_hours}h of focus time. "
            f"{report.back_to_back_count} back-to-back(s), {report.conflict_count} conflict(s)."
        )

    return {
        "score": report.score,
        "meeting_hours": report.meeting_hours,
        "focus_hours": report.focus_hours,
        "back_to_back_count": report.back_to_back_count,
        "conflict_count": report.conflict_count,
        "fragmentation_count": report.fragmentation_count,
        "issues": report.issues,
        "summary": ai_summary,
        "backend": _backend_name,
    }


class ChatRequest(BaseModel):
    message: str
    # For slot selection: include title and attendees to build proposal
    slot_title: Optional[str] = None
    slot_attendees: Optional[list[str]] = None


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    response: ChatMessage = _agent.chat(req.message)
    return {
        "role": response.role,
        "content": response.content,
        "proposal": _serialize_proposal(response.proposal),
        "slot_options": [_serialize_slot(s) for s in (response.slot_options or [])],
        "mode": response.mode,
    }


class SlotSelectRequest(BaseModel):
    slot: dict   # serialized SlotOption
    title: str
    attendees: list[str] = []


@app.post("/api/slot/select")
async def api_slot_select(req: SlotSelectRequest):
    """Turn a chosen slot into a full proposal."""
    slot = SlotOption(
        start=datetime.fromisoformat(req.slot["start"]),
        end=datetime.fromisoformat(req.slot["end"]),
        score=req.slot["score"],
        why=req.slot["why"],
    )
    proposal = _agent.create_slot_proposal(slot, req.title, req.attendees, 0)
    return {"proposal": _serialize_proposal(proposal)}


@app.post("/api/apply")
async def api_apply(req: ApplyRequest):
    ok, msg = _agent.apply_proposal(req.proposal_id, custom_title=req.custom_title)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    # Return updated events and health
    start, end = _week_range()
    events = get_store().list_events(start, end)
    report = health_score(events)
    return {
        "message": msg,
        "events": [_serialize_event(e) for e in events],
        "health": {
            "score": report.score,
            "meeting_hours": report.meeting_hours,
            "focus_hours": report.focus_hours,
            "back_to_back_count": report.back_to_back_count,
            "conflict_count": report.conflict_count,
            "issues": report.issues,
        },
    }


@app.post("/api/reject")
async def api_reject(req: RejectRequest):
    ok, msg = _agent.reject_proposal(req.proposal_id)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)
    return {"message": msg}


@app.post("/api/undo")
async def api_undo():
    ok, msg = _agent.undo_last()
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    start, end = _week_range()
    events = get_store().list_events(start, end)
    report = health_score(events)
    return {
        "message": msg,
        "events": [_serialize_event(e) for e in events],
        "health": {
            "score": report.score,
            "meeting_hours": report.meeting_hours,
            "focus_hours": report.focus_hours,
            "back_to_back_count": report.back_to_back_count,
            "conflict_count": report.conflict_count,
            "issues": report.issues,
        },
    }


def _parse_dt(dt_val: Optional[str]) -> Optional[datetime]:
    if not dt_val:
        return None
    s = str(dt_val).strip()
    if s.endswith("Z"):
        s = s[:-1]
    if "+" in s:
        s = s.split("+")[0]
    return datetime.fromisoformat(s).replace(tzinfo=None)


class ManualCreateEventRequest(BaseModel):
    title: str
    start: str
    end: str
    type: EventType = "meeting"
    description: Optional[str] = None


class ManualUpdateEventRequest(BaseModel):
    title: Optional[str] = None
    type: Optional[EventType] = None
    description: Optional[str] = None
    new_start: Optional[str] = None
    new_end: Optional[str] = None


@app.post("/api/events/manual")
async def api_manual_create_event(req: ManualCreateEventRequest):
    try:
        s = _parse_dt(req.start)
        en = _parse_dt(req.end)
        if not s or not en:
            raise ValueError("Start and end must be provided")
    except Exception as e:
        logger.error(f"Invalid datetime format: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {e}")

    ev = Event(
        title=req.title,
        start=s,
        end=en,
        type=req.type,
        description=req.description,
    )
    ok, msg = _agent.manual_create_event(ev)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    start, end = _week_range()
    events = get_store().list_events(start, end)
    conflicts = detect_conflicts(events)
    conflict_ids = {e1.id for e1, e2 in conflicts} | {e2.id for e1, e2 in conflicts}
    report = health_score(events)

    serialized = []
    for e in events:
        d = _serialize_event(e)
        d["has_conflict"] = e.id in conflict_ids
        serialized.append(d)

    return {
        "message": msg,
        "events": serialized,
        "health": {
            "score": report.score,
            "meeting_hours": report.meeting_hours,
            "focus_hours": report.focus_hours,
            "back_to_back_count": report.back_to_back_count,
            "conflict_count": report.conflict_count,
            "issues": report.issues,
        },
    }


@app.put("/api/events/{event_id}")
@app.put("/api/events/{event_id}/move")
async def api_manual_update_event(event_id: str, req: ManualUpdateEventRequest):
    try:
        s = _parse_dt(req.new_start) if req.new_start else None
        en = _parse_dt(req.new_end) if req.new_end else None
    except Exception as e:
        logger.error(f"Invalid datetime format: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid datetime format: {e}")

    ok, msg, updated = _agent.manual_update_event(
        event_id=event_id,
        new_start=s,
        new_end=en,
        title=req.title,
        event_type=req.type,
        description=req.description,
    )
    if not ok:
        raise HTTPException(status_code=404, detail=msg)

    start, end = _week_range()
    events = get_store().list_events(start, end)
    conflicts = detect_conflicts(events)
    conflict_ids = {e1.id for e1, e2 in conflicts} | {e2.id for e1, e2 in conflicts}
    report = health_score(events)

    serialized = []
    for e in events:
        d = _serialize_event(e)
        d["has_conflict"] = e.id in conflict_ids
        serialized.append(d)

    return {
        "message": msg,
        "events": serialized,
        "health": {
            "score": report.score,
            "meeting_hours": report.meeting_hours,
            "focus_hours": report.focus_hours,
            "back_to_back_count": report.back_to_back_count,
            "conflict_count": report.conflict_count,
            "issues": report.issues,
        },
    }
        "events": serialized,
        "health": {
            "score": report.score,
            "meeting_hours": report.meeting_hours,
            "focus_hours": report.focus_hours,
            "back_to_back_count": report.back_to_back_count,
            "conflict_count": report.conflict_count,
            "issues": report.issues,
        },
    }


@app.delete("/api/events/{event_id}")
async def api_manual_delete_event(event_id: str):
    ok, msg = _agent.manual_delete_event(event_id)
    if not ok:
        raise HTTPException(status_code=404, detail=msg)

    start, end = _week_range()
    events = get_store().list_events(start, end)
    conflicts = detect_conflicts(events)
    conflict_ids = {e1.id for e1, e2 in conflicts} | {e2.id for e1, e2 in conflicts}
    report = health_score(events)

    serialized = []
    for e in events:
        d = _serialize_event(e)
        d["has_conflict"] = e.id in conflict_ids
        serialized.append(d)

    return {
        "message": msg,
        "events": serialized,
        "health": {
            "score": report.score,
            "meeting_hours": report.meeting_hours,
            "focus_hours": report.focus_hours,
            "back_to_back_count": report.back_to_back_count,
            "conflict_count": report.conflict_count,
            "issues": report.issues,
        },
    }


@app.post("/api/reset")
async def api_reset():
    global _agent
    if _backend_name == "google":
        try:
            # Import and run seed for Google
            import seed_google
            seed_google.seed(_google_store)
            msg = "Google Calendar re-seeded with demo data."
        except Exception as e:
            msg = f"Google seed failed: {e}"
    else:
        _mock_store.reset()
        msg = "Mock data reset."
    _agent = KairosAgent(get_store())
    return {"message": msg}


@app.post("/api/backend")
async def api_backend(req: BackendRequest):
    global _backend_name, _agent
    _backend_name = req.backend
    _agent = KairosAgent(get_store())
    return {
        "backend": _backend_name,
        "google_authed": _google_store.is_authenticated(),
    }


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

@app.get("/auth/google")
async def auth_google():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        SCOPES = ["https://www.googleapis.com/auth/calendar"]
        if not Path("credentials.json").exists():
            raise HTTPException(
                status_code=400,
                detail="credentials.json not found. Download it from Google Cloud Console.",
            )
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
        with open("token.json", "w") as f:
            f.write(creds.to_json())
        # Refresh google store and agent
        global _backend_name, _agent
        _backend_name = "google"
        _google_store._service = None
        _agent = KairosAgent(get_store())
        return {"status": "authenticated", "message": "Google Calendar connected!"}
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="google-auth-oauthlib not installed. Run: pip install google-auth-oauthlib",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/status")
async def api_status():
    return {
        "backend": _backend_name,
        "google_authed": _google_store.is_authenticated(),
        "gemini_available": _agent._gemini_available,
        "gemini_model": _agent._model_name,
    }
