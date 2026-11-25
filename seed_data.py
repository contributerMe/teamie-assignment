"""
seed_data.py – Realistic demo week seeded relative to today.
Provides build_seed_events() and ATTENDEE_BUSY for the mock backend.
"""
from __future__ import annotations
from datetime import datetime, date, timedelta, time
from models import Event, Attendee

# Fake attendees used throughout the demo
PRIYA = Attendee(email="priya@example.com", name="Priya")
RAHUL = Attendee(email="rahul@example.com", name="Rahul")
ANANYA = Attendee(email="ananya@example.com", name="Ananya")
VIKRAM = Attendee(email="vikram@example.com", name="Vikram")
EXTERNAL_CLIENT = Attendee(email="john.smith@acmecorp.com", name="John Smith (Acme)")
ME = Attendee(email="me@mycompany.com", name="You")


def _this_monday() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def _dt(day_offset: int, h: int, m: int = 0) -> datetime:
    """Create a datetime relative to Monday of the current week."""
    monday = _this_monday()
    return datetime.combine(monday + timedelta(days=day_offset), time(h, m))


def build_seed_events() -> list[Event]:
    """
    Build a realistic 5-day week with:
    - Daily standups
    - 1:1 meetings
    - A 4-person planning meeting
    - An external client call
    - Back-to-back blocks
    - One double-booking (conflict)
    - One low-value status sync
    - Busy blocks for attendees
    """
    events: list[Event] = []

    # --- Monday (day 0) ---
    events += [
        Event(id="mon-standup", title="Daily Standup", start=_dt(0, 9, 30), end=_dt(0, 9, 45),
              attendees=[ME, PRIYA, RAHUL, ANANYA, VIKRAM], type="meeting"),
        Event(id="mon-1on1-priya", title="1:1 with Priya", start=_dt(0, 10), end=_dt(0, 10, 30),
              attendees=[ME, PRIYA], type="meeting"),
        Event(id="mon-focus", title="Deep Work: Quarterly Plan", start=_dt(0, 11), end=_dt(0, 13),
              attendees=[ME], type="focus"),
        # Back-to-back pair
        Event(id="mon-review", title="Product Review", start=_dt(0, 14), end=_dt(0, 15),
              attendees=[ME, PRIYA, RAHUL], type="meeting"),
        Event(id="mon-design", title="Design Sync", start=_dt(0, 15), end=_dt(0, 15, 45),
              attendees=[ME, ANANYA], type="meeting"),
        Event(id="mon-client", title="Client Call – Acme Corp", start=_dt(0, 16), end=_dt(0, 17),
              attendees=[ME, EXTERNAL_CLIENT], type="external"),
    ]

    # --- Tuesday (day 1) ---
    events += [
        Event(id="tue-standup", title="Daily Standup", start=_dt(1, 9, 30), end=_dt(1, 9, 45),
              attendees=[ME, PRIYA, RAHUL, ANANYA, VIKRAM], type="meeting"),
        # Double-booking conflict!
        Event(id="tue-conflict-a", title="Sprint Planning", start=_dt(1, 10), end=_dt(1, 11, 30),
              attendees=[ME, PRIYA, RAHUL, ANANYA, VIKRAM], type="meeting"),
        Event(id="tue-conflict-b", title="1:1 with Rahul", start=_dt(1, 10, 30), end=_dt(1, 11),
              attendees=[ME, RAHUL], type="meeting"),
        Event(id="tue-status-sync", title="Weekly Status Sync",
              start=_dt(1, 14), end=_dt(1, 15),
              attendees=[ME, PRIYA, RAHUL, ANANYA, VIKRAM], type="meeting",
              description="General status update – no set agenda"),
        Event(id="tue-focus", title="Focus Block", start=_dt(1, 15, 30), end=_dt(1, 17),
              attendees=[ME], type="focus"),
    ]

    # --- Wednesday (day 2) ---
    events += [
        Event(id="wed-standup", title="Daily Standup", start=_dt(2, 9, 30), end=_dt(2, 9, 45),
              attendees=[ME, PRIYA, RAHUL, ANANYA, VIKRAM], type="meeting"),
        Event(id="wed-planning", title="Quarterly Planning", start=_dt(2, 10), end=_dt(2, 12),
              attendees=[ME, PRIYA, RAHUL, ANANYA, VIKRAM], type="meeting"),
        Event(id="wed-1on1-ananya", title="1:1 with Ananya", start=_dt(2, 14), end=_dt(2, 14, 30),
              attendees=[ME, ANANYA], type="meeting"),
        Event(id="wed-focus", title="Focus Block", start=_dt(2, 15), end=_dt(2, 17),
              attendees=[ME], type="focus"),
    ]

    # --- Thursday (day 3) ---
    events += [
        Event(id="thu-standup", title="Daily Standup", start=_dt(3, 9, 30), end=_dt(3, 9, 45),
              attendees=[ME, PRIYA, RAHUL, ANANYA, VIKRAM], type="meeting"),
        Event(id="thu-1on1-vikram", title="1:1 with Vikram", start=_dt(3, 10), end=_dt(3, 10, 30),
              attendees=[ME, VIKRAM], type="meeting"),
        # Back-to-back triple
        Event(id="thu-review-a", title="Roadmap Review", start=_dt(3, 11), end=_dt(3, 12),
              attendees=[ME, PRIYA, ANANYA], type="meeting"),
        Event(id="thu-review-b", title="Hiring Panel", start=_dt(3, 12), end=_dt(3, 13),
              attendees=[ME, RAHUL, VIKRAM], type="meeting"),
        Event(id="thu-external", title="Vendor Call – CloudInc", start=_dt(3, 14), end=_dt(3, 14, 45),
              attendees=[ME, Attendee(email="sales@cloudinc.io", name="CloudInc Sales")], type="external"),
        Event(id="thu-retro", title="Team Retro", start=_dt(3, 15), end=_dt(3, 16),
              attendees=[ME, PRIYA, RAHUL, ANANYA, VIKRAM], type="meeting"),
    ]

    # --- Friday (day 4) ---
    events += [
        Event(id="fri-standup", title="Daily Standup", start=_dt(4, 9, 30), end=_dt(4, 9, 45),
              attendees=[ME, PRIYA, RAHUL, ANANYA, VIKRAM], type="meeting"),
        Event(id="fri-1on1-priya", title="1:1 with Priya (EOW)", start=_dt(4, 10), end=_dt(4, 10, 30),
              attendees=[ME, PRIYA], type="meeting"),
        Event(id="fri-focus", title="Focus Block", start=_dt(4, 11), end=_dt(4, 13),
              attendees=[ME], type="focus"),
        Event(id="fri-wrap", title="Weekly Wrap-up", start=_dt(4, 16), end=_dt(4, 16, 30),
              attendees=[ME, PRIYA, RAHUL], type="meeting"),
    ]

    return events


# Attendee busy times (relative to this week) for mock free/busy responses
def _build_attendee_busy() -> dict[str, list[tuple[datetime, datetime]]]:
    busy: dict[str, list[tuple[datetime, datetime]]] = {
        "priya@example.com": [
            (_dt(0, 9), _dt(0, 10)),        # Mon morning blocked
            (_dt(1, 11), _dt(1, 13)),       # Tue 11-1
            (_dt(2, 9), _dt(2, 11)),        # Wed morning
            (_dt(3, 14, 30), _dt(3, 17)),   # Thu afternoon
            (_dt(4, 15), _dt(4, 17)),       # Fri late afternoon
        ],
        "rahul@example.com": [
            (_dt(0, 14), _dt(0, 16)),
            (_dt(1, 9), _dt(1, 11)),
            (_dt(2, 10), _dt(2, 12)),
            (_dt(3, 10), _dt(3, 11, 30)),
        ],
        "ananya@example.com": [
            (_dt(0, 10), _dt(0, 12)),
            (_dt(1, 14), _dt(1, 15, 30)),
            (_dt(2, 15), _dt(2, 17)),
            (_dt(3, 11), _dt(3, 13)),
            (_dt(4, 10), _dt(4, 12)),
        ],
        "vikram@example.com": [
            (_dt(0, 15), _dt(0, 17)),
            (_dt(1, 10), _dt(1, 11)),
            (_dt(2, 14), _dt(2, 16)),
            (_dt(3, 9), _dt(3, 10, 30)),
        ],
    }
    return busy


ATTENDEE_BUSY = _build_attendee_busy()
