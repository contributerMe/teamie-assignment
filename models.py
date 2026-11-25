"""
models.py – Pydantic data models shared across the app.
"""
from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field
import uuid


EventType = Literal["meeting", "external", "focus", "personal", "other"]
ActionType = Literal["create", "update", "delete"]


class Attendee(BaseModel):
    email: str
    name: Optional[str] = None


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    start: datetime
    end: datetime
    attendees: list[Attendee] = []
    type: EventType = "meeting"
    location: Optional[str] = None
    description: Optional[str] = None
    # extra metadata
    is_recurring: bool = False
    organizer: Optional[str] = None

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)


class Change(BaseModel):
    action: ActionType
    event: Event            # the new state (or the event being deleted)
    before: Optional[Event] = None   # original state for updates


class Proposal(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    summary: str
    changes: list[Change]
    reasoning: str


class SlotOption(BaseModel):
    start: datetime
    end: datetime
    score: float            # 0-1, higher is better
    why: str


class HealthReport(BaseModel):
    score: int              # 0-100
    meeting_hours: float
    focus_hours: float
    back_to_back_count: int
    conflict_count: int
    fragmentation_count: int   # gaps < 30 min
    issues: list[str]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    proposal: Optional[Proposal] = None
    slot_options: Optional[list[SlotOption]] = None
    mode: Literal["gemini", "offline"] = "offline"


class ApplyRequest(BaseModel):
    proposal_id: str
    # for slot selection, the chosen slot index
    slot_index: Optional[int] = None
    custom_title: Optional[str] = None


class RejectRequest(BaseModel):
    proposal_id: str


class BackendRequest(BaseModel):
    backend: Literal["mock", "google"]
