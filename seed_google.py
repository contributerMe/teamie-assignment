"""
seed_google.py – Populates a Google Calendar with the same demo week.
Idempotent: deletes previously seeded events (tagged with created_by=ai-calendar-seed)
before re-inserting. Refuses to run against "primary" calendar.

Usage:
    python seed_google.py                   # uses GOOGLE_CALENDAR_ID from .env
    python seed_google.py my-calendar-id   # explicit calendar id
"""
from __future__ import annotations
import sys
import os
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SEED_TAG = "ai-calendar-seed"


def seed(store=None, calendar_id: str = None):
    from dotenv import load_dotenv
    load_dotenv()

    cal_id = calendar_id or os.getenv("GOOGLE_CALENDAR_ID", "primary")
    if cal_id == "primary":
        raise RuntimeError(
            "Refusing to seed the 'primary' calendar. "
            "Set GOOGLE_CALENDAR_ID to a dedicated test calendar ID."
        )

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    creds = None
    if Path("token.json").exists():
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        raise RuntimeError("Not authenticated. Run the app and visit /auth/google first.")

    service = build("calendar", "v3", credentials=creds)

    # --- Delete previously seeded events ---
    logger.info("Removing old seeded events...")
    page_token = None
    deleted = 0
    while True:
        resp = service.events().list(
            calendarId=cal_id,
            privateExtendedProperty=f"created_by={SEED_TAG}",
            pageToken=page_token,
            maxResults=250,
        ).execute()
        for item in resp.get("items", []):
            service.events().delete(calendarId=cal_id, eventId=item["id"]).execute()
            deleted += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    logger.info(f"Deleted {deleted} old seeded event(s).")

    # --- Build and insert fresh demo events ---
    from seed_data import build_seed_events
    events = build_seed_events()
    inserted = 0
    for ev in events:
        body = {
            "summary": ev.title,
            "start": {"dateTime": ev.start.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "UTC"},
            "end": {"dateTime": ev.end.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "UTC"},
            "attendees": [{"email": a.email} for a in ev.attendees],
            "extendedProperties": {
                "private": {"created_by": SEED_TAG}
            },
        }
        if ev.description:
            body["description"] = ev.description
        service.events().insert(calendarId=cal_id, body=body, sendUpdates="none").execute()
        inserted += 1

    logger.info(f"Inserted {inserted} demo event(s) into calendar '{cal_id}'.")
    return inserted


if __name__ == "__main__":
    cal_id = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        n = seed(calendar_id=cal_id)
        print(f"✅ Seeded {n} events.")
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
