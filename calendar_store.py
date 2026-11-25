"""
calendar_store.py – Unified interface for calendar data sources.
Provides MockCalendarStore and GoogleCalendarStore, both implementing the same API.
"""
from __future__ import annotations
import os
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, date, timezone
from typing import Optional

from models import Event, Change, Attendee, EventType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class CalendarStore(ABC):
    @abstractmethod
    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        ...

    @abstractmethod
    def get_free_busy(
        self, attendees: list[str], start: datetime, end: datetime
    ) -> dict[str, list[tuple[datetime, datetime]]]:
        ...

    @abstractmethod
    def apply_changes(self, changes: list[Change]) -> list[Event]:
        """Apply changes and return the resulting events."""
        ...


# ---------------------------------------------------------------------------
# Mock store – in-memory, seeded with realistic demo data
# ---------------------------------------------------------------------------

class MockCalendarStore(CalendarStore):
    def __init__(self):
        self._events: dict[str, Event] = {}
        self._seed()

    def _seed(self):
        from seed_data import build_seed_events
        for event in build_seed_events():
            self._events[event.id] = event

    def reset(self):
        self._events.clear()
        self._seed()

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        result = []
        for e in self._events.values():
            # Normalize timezone
            es = e.start.replace(tzinfo=None)
            ee = e.end.replace(tzinfo=None)
            s = start.replace(tzinfo=None)
            en = end.replace(tzinfo=None)
            if es < en and ee > s:
                result.append(e)
        return sorted(result, key=lambda x: x.start)

    def get_free_busy(
        self, attendees: list[str], start: datetime, end: datetime
    ) -> dict[str, list[tuple[datetime, datetime]]]:
        """Return busy periods from the mock seed data for known attendees."""
        from seed_data import ATTENDEE_BUSY
        result: dict[str, list[tuple[datetime, datetime]]] = {}
        s = start.replace(tzinfo=None)
        en = end.replace(tzinfo=None)
        for email in attendees:
            busy = ATTENDEE_BUSY.get(email, None)
            if busy is None:
                # Unknown attendee – treat as unavailable unknown
                result[email] = []  # empty = unknown, not free
            else:
                result[email] = [
                    (bs, be) for bs, be in busy if bs < en and be > s
                ]
        return result

    def apply_changes(self, changes: list[Change]) -> list[Event]:
        applied = []
        for change in changes:
            if change.action == "create":
                self._events[change.event.id] = change.event
                applied.append(change.event)
            elif change.action == "update":
                self._events[change.event.id] = change.event
                applied.append(change.event)
            elif change.action == "delete":
                self._events.pop(change.event.id, None)
        return applied


# ---------------------------------------------------------------------------
# Google Calendar store
# ---------------------------------------------------------------------------

class GoogleCalendarStore(CalendarStore):
    def __init__(self, calendar_id: str = "primary"):
        self.calendar_id = calendar_id
        self._service = None
        self._creds = None

    def _get_service(self):
        if self._service:
            return self._service
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build

            SCOPES = ["https://www.googleapis.com/auth/calendar"]
            creds = None
            if os.path.exists("token.json"):
                creds = Credentials.from_authorized_user_file("token.json", SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open("token.json", "w") as f:
                    f.write(creds.to_json())
            if not creds or not creds.valid:
                raise RuntimeError("Not authenticated. Run /auth/google first.")
            self._creds = creds
            self._service = build("calendar", "v3", credentials=creds)
            return self._service
        except ImportError as e:
            raise RuntimeError(f"Google API libraries not installed: {e}")

    def is_authenticated(self) -> bool:
        try:
            self._get_service()
            return True
        except Exception:
            return False

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        try:
            service = self._get_service()
            # Get the calendar's timezone
            cal_meta = service.calendars().get(calendarId=self.calendar_id).execute()
            tz_name = cal_meta.get("timeZone", "UTC")

            result = service.events().list(
                calendarId=self.calendar_id,
                timeMin=start.isoformat() + "Z",
                timeMax=end.isoformat() + "Z",
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            ).execute()

            events = []
            for item in result.get("items", []):
                # Skip all-day events and declined events
                if "dateTime" not in item.get("start", {}):
                    continue
                self_status = next(
                    (a.get("responseStatus") for a in item.get("attendees", [])
                     if a.get("self")), None
                )
                if self_status == "declined":
                    continue

                ev = _gcal_item_to_event(item)
                if ev:
                    events.append(ev)
            return events
        except Exception as e:
            logger.error(f"GCal list_events failed: {e}")
            return []

    def get_free_busy(
        self, attendees: list[str], start: datetime, end: datetime
    ) -> dict[str, list[tuple[datetime, datetime]]]:
        try:
            service = self._get_service()
            body = {
                "timeMin": start.isoformat() + "Z",
                "timeMax": end.isoformat() + "Z",
                "items": [{"id": email} for email in attendees],
            }
            resp = service.freebusy().query(body=body).execute()
            result: dict[str, list[tuple[datetime, datetime]]] = {}
            for email in attendees:
                cal_data = resp.get("calendars", {}).get(email, {})
                if "errors" in cal_data:
                    # Cannot see this person's calendar
                    result[email] = []
                else:
                    busy_periods = []
                    for period in cal_data.get("busy", []):
                        bs = datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
                        be = datetime.fromisoformat(period["end"].replace("Z", "+00:00"))
                        busy_periods.append((bs.replace(tzinfo=None), be.replace(tzinfo=None)))
                    result[email] = busy_periods
            return result
        except Exception as e:
            logger.error(f"GCal freebusy failed: {e}")
            return {email: [] for email in attendees}

    def apply_changes(self, changes: list[Change]) -> list[Event]:
        service = self._get_service()
        send_updates = "all" if os.getenv("SEND_INVITES", "false").lower() == "true" else "none"
        applied = []
        for change in changes:
            try:
                if change.action == "create":
                    body = _event_to_gcal_body(change.event)
                    resp = service.events().insert(
                        calendarId=self.calendar_id,
                        body=body,
                        sendUpdates=send_updates,
                    ).execute()
                    ev = _gcal_item_to_event(resp) or change.event
                    applied.append(ev)
                elif change.action == "update":
                    body = _event_to_gcal_body(change.event)
                    resp = service.events().patch(
                        calendarId=self.calendar_id,
                        eventId=change.event.id,
                        body=body,
                        sendUpdates=send_updates,
                    ).execute()
                    ev = _gcal_item_to_event(resp) or change.event
                    applied.append(ev)
                elif change.action == "delete":
                    service.events().delete(
                        calendarId=self.calendar_id,
                        eventId=change.event.id,
                        sendUpdates=send_updates,
                    ).execute()
            except Exception as e:
                logger.error(f"GCal apply_change ({change.action}) failed: {e}")
        return applied


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_event_type(item: dict) -> EventType:
    attendees = item.get("attendees", [])
    title = (item.get("summary") or "").lower()
    organizer_domain = (item.get("organizer", {}).get("email") or "").split("@")[-1]

    if any("focus" in title or "deep work" in title or "no meetings" in title
           for _ in [title]):
        return "focus"

    if attendees:
        # Check for external attendees
        for a in attendees:
            email = a.get("email", "")
            domain = email.split("@")[-1]
            if domain and domain != organizer_domain and not a.get("self"):
                return "external"
        return "meeting"

    return "personal"


def _gcal_item_to_event(item: dict) -> Optional[Event]:
    try:
        start_str = item["start"].get("dateTime") or item["start"].get("date")
        end_str = item["end"].get("dateTime") or item["end"].get("date")
        if not start_str or not end_str:
            return None

        # Parse and strip timezone for internal use
        start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        start = start.replace(tzinfo=None)
        end = end.replace(tzinfo=None)

        attendees = [
            Attendee(email=a.get("email", ""), name=a.get("displayName"))
            for a in item.get("attendees", [])
        ]

        return Event(
            id=item.get("id", ""),
            title=item.get("summary", "Untitled"),
            start=start,
            end=end,
            attendees=attendees,
            type=_infer_event_type(item),
            location=item.get("location"),
            description=item.get("description"),
            organizer=item.get("organizer", {}).get("email"),
        )
    except Exception as e:
        logger.warning(f"Could not parse GCal event: {e}")
        return None


def _event_to_gcal_body(event: Event) -> dict:
    body: dict = {
        "summary": event.title,
        "start": {"dateTime": event.start.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "UTC"},
        "end": {"dateTime": event.end.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "UTC"},
        "extendedProperties": {
            "private": {"created_by": "ai-calendar"}
        },
    }
    if event.attendees:
        body["attendees"] = [{"email": a.email} for a in event.attendees]
    if event.location:
        body["location"] = event.location
    if event.description:
        body["description"] = event.description
    return body
